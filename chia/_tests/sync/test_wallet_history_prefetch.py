"""Exercise history preparation with real wallet stores and isolated databases."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import logging
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gold_rs import G1Element

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.protocols.shared_protocol import Capability
from chia.protocols.wallet_protocol import (
    CoinState,
    PuzzleSolutionResponse,
    RejectBlockHeaders,
    RespondChildren,
    RespondPuzzleSolution,
)
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.serialized_program import SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.db_wrapper import DBWrapper2
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64
from chia.wallet.derivation_record import DerivationRecord
from chia.wallet.util.peer_request_cache import PeerRequestCache
from chia.wallet.util.wallet_sync_utils import PeerRequestException
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.wallet_coin_store import WalletCoinStore
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_puzzle_store import WalletPuzzleStore
from chia.wallet.wallet_retry_store import WalletRetryStore
from chia.wallet.wallet_state_manager import WalletStateManager
from chia.wallet.wallet_transaction_store import WalletTransactionStore


@asynccontextmanager
async def history_fixture(count=1, latency=0, owned_children=False):
    with tempfile.TemporaryDirectory(prefix="wallet-history-test-") as directory:
        async with DBWrapper2.managed(Path(directory) / "wallet.sqlite") as database:
            manager = WalletStateManager()
            manager.db_wrapper = database
            manager.log = logging.getLogger("wallet-history-test")
            manager.constants = DEFAULT_CONSTANTS
            manager.coin_store = await WalletCoinStore.create(database)
            manager.tx_store = await WalletTransactionStore.create(database)
            manager.retry_store = await WalletRetryStore.create(database)
            manager.puzzle_store = await WalletPuzzleStore.create(database)
            manager.trade_manager = SimpleNamespace(get_coins_of_interest=AsyncMock(return_value=set()))
            manager.blockchain = SimpleNamespace(synced_weight_proof=object(), clean_block_records=AsyncMock())
            manager.wallets = {uint32(1): SimpleNamespace(type=lambda: WalletType.STANDARD_WALLET)}
            puzzle = Program.to(1)
            await manager.puzzle_store.add_derivation_paths([
                DerivationRecord(
                    uint32(0), puzzle.get_tree_hash(), G1Element(), WalletType.STANDARD_WALLET, uint32(1), False
                )
            ])
            states = [
                CoinState(
                    Coin(std_hash(f"parent-{i}".encode()), puzzle.get_tree_hash(), uint64(1001 + i)),
                    uint32(1000 + i),
                    uint32(100 + i),
                )
                for i in range(count)
            ]
            children = {
                state.coin.name(): [
                    CoinState(
                        Coin(
                            state.coin.name(),
                            puzzle.get_tree_hash() if owned_children else std_hash(b"external-destination"),
                            uint64(state.coin.amount - 1),
                        ),
                        None,
                        state.spent_height,
                    )
                ]
                for state in states
            }
            cache = PeerRequestCache()
            for state in states:
                cache.add_to_states_validated(state)
                for height in (state.created_height, state.spent_height):
                    cache.add_to_blocks(SimpleNamespace(
                        height=height,
                        is_transaction_block=True,
                        foliage_transaction_block=SimpleNamespace(timestamp=uint64(1700000000 + height)),
                    ))
                for child in children[state.coin.name()]:
                    cache.add_to_states_validated(child)

            node = WalletNode(config={}, root_path=Path(directory), constants=DEFAULT_CONSTANTS)
            node._wallet_state_manager = manager
            node._shut_down = False
            manager.wallet_node = node
            manager.filter_spam = AsyncMock(side_effect=lambda items: items)
            node.update_ui = AsyncMock()
            metrics = SimpleNamespace(
                active=0, max_active=0, calls=0, writer_calls=0, child_calls=0, puzzle_calls=0, local_calls=0
            )
            peer = SimpleNamespace(
                peer_node_id=std_hash(b"public-peer"), closed=False,
                peer_info=SimpleNamespace(host="public-test-peer"), peer_capabilities=[Capability.BLOCK_HEADERS],
                close=AsyncMock(),
            )
            local = SimpleNamespace(
                peer_node_id=std_hash(b"unsynced-local-peer"), closed=False,
                peer_info=SimpleNamespace(host="127.0.0.1"), peer_capabilities=[Capability.BLOCK_HEADERS],
            )

            async def request(_api, request, **_kwargs):
                metrics.calls += 1
                metrics.active += 1
                metrics.max_active = max(metrics.max_active, metrics.active)
                if database._current_writer == asyncio.current_task():
                    metrics.writer_calls += 1
                try:
                    await asyncio.sleep(latency)
                    if type(request).__name__ == "RequestChildren":
                        metrics.child_calls += 1
                        return RespondChildren(children[request.coin_name])
                    if type(request).__name__ == "RequestPuzzleSolution":
                        metrics.puzzle_calls += 1
                        return RespondPuzzleSolution(PuzzleSolutionResponse(
                            request.coin_name, request.height,
                            SerializedProgram.from_program(puzzle), SerializedProgram.from_program(Program.to([])),
                        ))
                    raise AssertionError(f"Unexpected public request: {request!r}")
                finally:
                    metrics.active -= 1

            async def local_request(_api, request, **_kwargs):
                metrics.local_calls += 1
                if database._current_writer == asyncio.current_task():
                    metrics.writer_calls += 1
                await asyncio.sleep(latency)
                return RejectBlockHeaders(request.start_height, request.end_height)

            peer.call_api = AsyncMock(side_effect=request)
            local.call_api = AsyncMock(side_effect=local_request)
            node._server = SimpleNamespace(
                all_connections={peer.peer_node_id: peer, local.peer_node_id: local},
                get_connections=lambda *_: [local, peer],
                is_trusted_peer=lambda candidate, _: candidate is local,
            )
            node.peer_caches[peer.peer_node_id] = cache
            node.peer_caches[local.peer_node_id] = PeerRequestCache()
            yield SimpleNamespace(
                manager=manager, node=node, peer=peer, local=local, states=states,
                children=children, cache=cache, metrics=metrics, database=database,
            )


async def records(fixture):
    coins = await fixture.manager.coin_store.get_coin_records()
    transactions = await fixture.manager.tx_store.get_all_transactions()
    normalized = []
    for transaction in transactions:
        data = transaction.to_json_dict()
        # Incoming record identifiers were already random in the original implementation.
        data.pop("name")
        normalized.append(data)
    return {
        "coins": sorted(repr(record) for record in coins.coin_id_to_record.values()),
        "transactions": sorted(normalized, key=lambda item: (
            item["confirmed_at_height"], item["type"], item["amount"]
        )),
        "retry": await fixture.manager.retry_store.get_all_states_to_retry(),
    }


class HistoryPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_application_matches_original_store_results(self):
        async with history_fixture(4) as original:
            self.assertTrue(await original.manager.add_coin_states(original.states, original.peer, None))
            expected = await records(original)
            self.assertGreater(original.metrics.writer_calls, 0)
        async with history_fixture(4) as optimized:
            prepared = await optimized.manager.prepare_coin_state_data(optimized.states, optimized.peer, None)
            self.assertEqual(len(prepared), 4)
            self.assertTrue(await optimized.manager.add_coin_states(
                optimized.states, optimized.peer, None, prepared=prepared
            ))
            self.assertEqual(expected, await records(optimized))
            self.assertEqual(optimized.metrics.writer_calls, 0)
            self.assertEqual(optimized.metrics.child_calls, 4)
            self.assertEqual(optimized.metrics.puzzle_calls, 4)
            self.assertEqual(optimized.metrics.local_calls, 0)

    async def test_pipeline_prepares_outside_writer_and_uses_validated_children(self):
        async with history_fixture(12, latency=0.001) as fixture:
            self.assertTrue(await fixture.node.add_states_from_peer(fixture.states, fixture.peer))
            result = await records(fixture)
            self.assertEqual(len(result["coins"]), 12)
            self.assertEqual(len(result["transactions"]), 24)
            self.assertEqual(fixture.metrics.writer_calls, 0)
            self.assertEqual(fixture.metrics.child_calls, 12)
            self.assertEqual(fixture.metrics.puzzle_calls, 12)

    async def test_concurrency_is_bounded_across_batches(self):
        async with history_fixture(24, latency=0.005) as fixture:
            maps = await asyncio.gather(*(
                fixture.manager.prepare_coin_state_data(fixture.states[i:i + 8], fixture.peer, None)
                for i in range(0, 24, 8)
            ))
            self.assertEqual(sum(map(len, maps)), 24)
            self.assertGreater(fixture.metrics.max_active, 1)
            self.assertLessEqual(fixture.metrics.max_active, 6)

    async def test_no_prefetch_for_reorg_trusted_or_missing_weight_proof(self):
        for case in ("reorg", "trusted", "no-weight-proof"):
            async with history_fixture() as fixture:
                peer = fixture.local if case == "trusted" else fixture.peer
                fork = uint32(25) if case == "reorg" else None
                if case == "no-weight-proof":
                    fixture.manager.blockchain.synced_weight_proof = None
                self.assertEqual(await fixture.manager.prepare_coin_state_data(fixture.states, peer, fork), {})
                self.assertEqual(fixture.metrics.calls, 0)

    async def test_no_prefetch_for_unspent_unknown_special_trade_or_existing_coins(self):
        for case in ("unspent", "reorged", "unknown", "special", "trade", "existing"):
            async with history_fixture() as fixture:
                state = fixture.states[0]
                if case == "unspent":
                    state = CoinState(state.coin, None, state.created_height)
                elif case == "reorged":
                    state = CoinState(state.coin, None, None)
                elif case in ("unknown", "special"):
                    from chia.wallet.util.wallet_types import WalletIdentifier
                    identifier = None if case == "unknown" else WalletIdentifier(uint32(2), WalletType.POOLING_WALLET)
                    fixture.manager.puzzle_store.get_wallet_identifier_for_puzzle_hash = AsyncMock(
                        return_value=identifier
                    )
                elif case == "trade":
                    fixture.manager.trade_manager.get_coins_of_interest.return_value = {state.coin.name()}
                elif case == "existing":
                    await fixture.manager.coin_store.add_coin_record(WalletCoinRecord(
                        state.coin, state.created_height, uint32(0), False, False, WalletType.STANDARD_WALLET, 1
                    ))
                self.assertEqual(await fixture.manager.prepare_coin_state_data([state], fixture.peer, None), {})
                self.assertEqual(fixture.metrics.calls, 0)

    async def test_unknown_timestamp_falls_back_without_fetching_children(self):
        async with history_fixture() as fixture:
            fixture.node.get_timestamp_for_height_from_peer = AsyncMock(return_value=None)
            self.assertEqual(await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None), {})
            self.assertEqual(fixture.metrics.child_calls, 0)

    async def test_owned_children_do_not_request_puzzle_solution(self):
        async with history_fixture(owned_children=True) as fixture:
            prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            self.assertEqual(len(prepared), 1)
            self.assertIsNone(next(iter(prepared.values())).spend)
            self.assertEqual(fixture.metrics.puzzle_calls, 0)
            self.assertTrue(await fixture.manager.add_coin_states(
                fixture.states, fixture.peer, None, prepared=prepared
            ))
            self.assertEqual(fixture.metrics.writer_calls, 0)

    async def test_failed_prefetch_falls_back_to_original_retry_store(self):
        async with history_fixture() as fixture:
            fixture.peer.call_api.side_effect = PeerRequestException("test unavailable")
            prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            self.assertEqual(prepared, {})
            await fixture.manager.add_coin_states(fixture.states, fixture.peer, None, prepared=prepared)
            retry = await fixture.manager.retry_store.get_all_states_to_retry()
            self.assertEqual([item[0] for item in retry], fixture.states)
            self.assertEqual((await records(fixture))["coins"], [])

    async def test_db_read_failure_during_prefetch_keeps_fallback(self):
        async with history_fixture() as fixture:
            fixture.manager.coin_store.get_coin_record = AsyncMock(side_effect=RuntimeError("test read failure"))
            self.assertEqual(await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None), {})

    async def test_cancellation_cleans_up_prefetch_tasks(self):
        async with history_fixture(12, latency=0.2) as fixture:
            task = asyncio.create_task(
                fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            )
            while fixture.metrics.active == 0:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(fixture.metrics.active, 0)
            self.assertEqual((await records(fixture))["coins"], [])

    async def test_closed_peer_and_shutdown_do_not_prefetch(self):
        for case in ("closed", "shutdown"):
            async with history_fixture() as fixture:
                if case == "closed":
                    fixture.peer.closed = True
                else:
                    fixture.node._shut_down = True
                self.assertEqual(await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None), {})
                self.assertEqual(fixture.metrics.calls, 0)

    async def test_changed_peer_state_proof_or_trade_discards_preparation(self):
        for case in ("peer", "state", "proof", "trade", "fork"):
            async with history_fixture() as fixture:
                prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
                key, value = next(iter(prepared.items()))
                if case == "peer":
                    prepared[key] = replace(value, peer_id=std_hash(b"another-peer"))
                elif case == "state":
                    prepared[key] = replace(value, state=CoinState(
                        value.state.coin, uint32(2000), value.state.created_height
                    ))
                elif case == "proof":
                    fixture.manager.blockchain.synced_weight_proof = object()
                elif case == "trade":
                    fixture.manager.trade_manager.get_coins_of_interest.return_value = {value.state.coin.name()}
                    fixture.manager.trade_manager.coins_of_interest_farmed = AsyncMock()
                await fixture.manager.add_coin_states(
                    fixture.states, fixture.peer, uint32(50) if case == "fork" else None, prepared=prepared
                )
                self.assertEqual(fixture.metrics.child_calls, 2, case)
                self.assertGreater(fixture.metrics.writer_calls, 0, case)

    async def test_existing_record_discards_preparation(self):
        async with history_fixture() as fixture:
            prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            state = fixture.states[0]
            await fixture.manager.coin_store.add_coin_record(WalletCoinRecord(
                state.coin, state.created_height, uint32(0), False, False, WalletType.STANDARD_WALLET, 1
            ))
            await fixture.manager.add_coin_states(fixture.states, fixture.peer, None, prepared=prepared)
            self.assertEqual(fixture.metrics.child_calls, 2)
            self.assertTrue((await fixture.manager.coin_store.get_coin_record(state.coin.name())).spent)

    async def test_invalid_parent_does_not_prefetch_or_apply(self):
        async with history_fixture() as fixture:
            fixture.node.validate_received_state_from_peer = AsyncMock(return_value=False)
            fixture.manager.prepare_coin_state_data = AsyncMock(return_value={})
            await fixture.node.add_states_from_peer(fixture.states, fixture.peer)
            fixture.manager.prepare_coin_state_data.assert_not_awaited()
            self.assertEqual((await records(fixture))["coins"], [])

    async def test_child_validation_is_still_called(self):
        async with history_fixture() as fixture:
            fixture.node.validate_received_state_from_peer = AsyncMock(return_value=False)
            prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            fixture.node.validate_received_state_from_peer.assert_awaited_once()
            self.assertEqual(next(iter(prepared.values())).children, ())

    async def test_invalid_puzzle_is_not_prepared(self):
        async with history_fixture() as fixture:
            request = fixture.peer.call_api.side_effect

            async def invalid_solution(api, message, **kwargs):
                if type(message).__name__ == "RequestPuzzleSolution":
                    return RespondPuzzleSolution(PuzzleSolutionResponse(
                        message.coin_name, message.height,
                        SerializedProgram.from_program(Program.to(2)),
                        SerializedProgram.from_program(Program.to([])),
                    ))
                return await request(api, message, **kwargs)

            fixture.peer.call_api.side_effect = invalid_solution
            self.assertEqual(await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None), {})

    async def test_failed_application_is_not_reported_as_sync_success(self):
        async with history_fixture() as fixture:
            fixture.manager.add_coin_states = AsyncMock(return_value=False)
            fixture.node.log = Mock()
            self.assertFalse(await fixture.node.add_states_from_peer(fixture.states, fixture.peer))
            logs = [str(call.args[0]) for call in fixture.node.log.info.call_args_list]
            self.assertFalse(any("batch processed" in message for message in logs))

    async def test_failed_batch_is_not_lost_when_finished_tasks_are_pruned(self):
        async with history_fixture(320) as fixture:
            fixture.manager.prepare_coin_state_data = AsyncMock(return_value={})
            fixture.manager.add_coin_states = AsyncMock(return_value=False)
            self.assertFalse(await fixture.node.add_states_from_peer(fixture.states, fixture.peer))

    async def test_preparation_error_requires_sync_retry(self):
        async with history_fixture() as fixture:
            fixture.manager.prepare_coin_state_data = AsyncMock(side_effect=RuntimeError("test preparation error"))
            self.assertFalse(await fixture.node.add_states_from_peer(fixture.states, fixture.peer))

    async def test_disconnected_peer_cannot_commit_prepared_batch(self):
        async with history_fixture() as fixture:
            prepared = await fixture.manager.prepare_coin_state_data(fixture.states, fixture.peer, None)
            fixture.peer.closed = True
            self.assertFalse(await fixture.manager.add_coin_states(
                fixture.states, fixture.peer, None, prepared=prepared
            ))
            self.assertEqual((await records(fixture))["coins"], [])


async def benchmark(count=40, latency=0.01):
    async with history_fixture(count=count, latency=latency) as fixture:
        started = time.monotonic()
        success = await fixture.node.add_states_from_peer(fixture.states, fixture.peer)
        elapsed = time.monotonic() - started
        result = await records(fixture)
        return {
            "seconds": round(elapsed, 4),
            "success": success,
            "coin_records": len(result["coins"]),
            "transaction_records": len(result["transactions"]),
            "network": vars(fixture.metrics),
            "records": result,
        }


class HeaderDownloadTests(unittest.IsolatedAsyncioTestCase):
    def response(self, start, end):
        return SimpleNamespace(header_blocks=[SimpleNamespace(height=height) for height in range(start, end + 1)])

    async def test_parallel_ranges_preserve_order_boundaries_and_global_limit(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()
        active = 0
        maximum = 0
        calls = []

        async def fetch(_peers, start, end):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            calls.append((start, end))
            await asyncio.sleep(0.005 if start % 64 else 0.015)
            active -= 1
            return self.response(start, end)

        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", side_effect=fetch):
            first, second = await asyncio.gather(
                fetch_header_blocks_in_range(uint32(5), uint32(320), cache, []),
                fetch_header_blocks_in_range(uint32(325), uint32(639), cache, []),
            )
        self.assertEqual([block.height for block in first], list(range(5, 321)))
        self.assertEqual([block.height for block in second], list(range(325, 640)))
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)
        self.assertEqual(len(calls), 21)

    async def test_overlapping_validators_share_pending_requests(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()

        async def fetch(_peers, start, end):
            await asyncio.sleep(0.005)
            return self.response(start, end)

        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", side_effect=fetch) as mocked:
            results = await asyncio.gather(*(
                fetch_header_blocks_in_range(uint32(0), uint32(95), cache, [])
                for _ in range(5)
            ))
            self.assertEqual(mocked.await_count, 3)
            again = await fetch_header_blocks_in_range(uint32(0), uint32(95), cache, [])
            self.assertEqual(mocked.await_count, 3)
        self.assertEqual([block.height for block in again], list(range(96)))
        self.assertTrue(all([block.height for block in result] == list(range(96)) for result in results))

    async def test_cancelling_one_validator_preserves_shared_download(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fetch(_peers, start, end):
            entered.set()
            await release.wait()
            return self.response(start, end)

        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", side_effect=fetch) as mocked:
            first = asyncio.create_task(fetch_header_blocks_in_range(uint32(0), uint32(31), cache, []))
            await entered.wait()
            second = asyncio.create_task(fetch_header_blocks_in_range(uint32(0), uint32(31), cache, []))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(cache.get_block_request(uint32(0), uint32(31)).cancelled())
            release.set()
            result = await second
            self.assertEqual([block.height for block in result], list(range(32)))
            self.assertEqual(mocked.await_count, 1)

    async def test_failed_and_empty_requests_are_retried(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        for failure in (None, ConnectionError("test connection loss")):
            cache = PeerRequestCache()
            with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", new=AsyncMock(
                side_effect=[failure, self.response(0, 31)]
            )) as mocked:
                if failure is None:
                    self.assertIsNone(await fetch_header_blocks_in_range(uint32(0), uint32(31), cache, []))
                else:
                    with self.assertRaises(ConnectionError):
                        await fetch_header_blocks_in_range(uint32(0), uint32(31), cache, [])
                result = await fetch_header_blocks_in_range(uint32(0), uint32(31), cache, [])
                self.assertEqual(len(result), 32)
                self.assertEqual(mocked.await_count, 2)

    async def test_cancelled_cache_entry_is_retried(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()
        task = asyncio.create_task(asyncio.sleep(5))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cache.add_to_block_requests(uint32(0), uint32(31), task)
        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", new=AsyncMock(
            return_value=self.response(0, 31)
        )) as mocked:
            self.assertEqual(len(await fetch_header_blocks_in_range(uint32(0), uint32(31), cache, [])), 32)
            mocked.assert_awaited_once()

    async def test_missing_duplicate_or_wrong_order_headers_fail_and_retry(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        for heights in ([0], [0, 0, 2], [2, 1, 0], []):
            cache = PeerRequestCache()
            invalid = SimpleNamespace(header_blocks=[SimpleNamespace(height=height) for height in heights])
            with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", new=AsyncMock(
                side_effect=[invalid, self.response(0, 2)]
            )) as mocked:
                with self.assertRaises(PeerRequestException):
                    await fetch_header_blocks_in_range(uint32(0), uint32(2), cache, [])
                result = await fetch_header_blocks_in_range(uint32(0), uint32(2), cache, [])
                self.assertEqual([block.height for block in result], [0, 1, 2])
                self.assertEqual(mocked.await_count, 2)

    async def test_reorg_discards_ranges_above_fork_point(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()
        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", new=AsyncMock(
            side_effect=lambda _peers, start, end: self.response(start, end)
        )) as mocked:
            await fetch_header_blocks_in_range(uint32(0), uint32(63), cache, [])
            self.assertEqual(mocked.await_count, 2)
            cache.clear_after_height(31)
            await fetch_header_blocks_in_range(uint32(0), uint32(63), cache, [])
            self.assertEqual(mocked.await_count, 3)

    async def test_genesis_and_single_height_ranges(self):
        from chia.wallet.util.wallet_sync_utils import fetch_header_blocks_in_range
        cache = PeerRequestCache()
        with patch("chia.wallet.util.wallet_sync_utils._fetch_header_blocks_inner", new=AsyncMock(
            side_effect=lambda _peers, start, end: self.response(start, end)
        )):
            self.assertEqual(
                [block.height for block in await fetch_header_blocks_in_range(uint32(0), uint32(0), cache, [])], [0]
            )
            self.assertEqual(
                [block.height for block in await fetch_header_blocks_in_range(uint32(35), uint32(35), cache, [])], [35]
            )
