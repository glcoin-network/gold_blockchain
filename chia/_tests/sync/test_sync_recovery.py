"""Exercise real recovery methods without a running network or wallet database."""

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gold_rs import AugSchemeMPL

from chia.consensus.blockchain import Blockchain
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.full_node import FullNode
from chia.full_node.full_node_api import FullNodeAPI
from chia.protocols.full_node_protocol import RequestProofOfWeight, RespondProofOfWeight
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.protocols.wallet_protocol import NewPeakWallet, RespondBlockHeader
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.server.outbound_message import Message, NodeType
from chia.server.ws_connection import WSGoldConnection
from chia.types.peer_info import PeerInfo
from chia.util.hash import std_hash
from chia.util.ints import uint8, uint16, uint32, uint64, uint128
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_node_api import WalletNodeAPI
from chia.wallet.wallet_weight_proof_handler import WalletWeightProofHandler


PEAK_HASH = b"\x11" * 32


class WeightProofCoefficientTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_recent_coefficients_preserve_genesis_boundary(self):
        chain = Blockchain.__new__(Blockchain)
        chain.constants = DEFAULT_CONSTANTS
        genesis = SimpleNamespace(
            height=uint32(0),
            prev_header_hash=chain.constants.GENESIS_CHALLENGE,
            reward_chain_block=SimpleNamespace(
                proof_of_space=SimpleNamespace(farmer_public_key=bytes(32)),
            ),
        )
        first_block = SimpleNamespace(
            height=uint32(1),
            prev_header_hash=chain.constants.GENESIS_CHALLENGE,
            reward_chain_block=SimpleNamespace(
                proof_of_space=SimpleNamespace(farmer_public_key=bytes(32)),
            ),
        )

        self.assertEqual(
            await chain.get_farmer_difficulty_coeffs([genesis, first_block]),
            [Decimal(20), Decimal(20)],
        )

    async def test_batch_recent_coefficients_match_scalar_formula(self):
        chain = Blockchain.__new__(Blockchain)
        chain.constants = DEFAULT_CONSTANTS.replace(
            STAKING_ESTIMATE_BLOCK_RANGE=uint32(10),
            DIFFICULTY_CONSTANT_FACTOR=uint128(2**57),
            NUMBER_ZERO_BITS_PLOT_FILTER=uint8(0),
        )
        chain._peak_height = uint32(64)
        hashes = {}
        records = {}
        farmers = [AugSchemeMPL.key_gen(bytes([index]) * 32).get_g1() for index in range(1, 5)]
        previous = b"\x00" * 32
        for height in range(1, 65):
            header_hash = std_hash(f"weight-proof-coeff-{height}".encode())
            hashes[height] = header_hash
            records[header_hash] = SimpleNamespace(
                header_hash=header_hash,
                prev_hash=previous,
                height=uint32(height),
                weight=uint128(height * 1000),
                total_iters=uint128(height * 10_000),
                farmer_public_key=farmers[height % len(farmers)],
            )
            previous = header_hash

        chain._Blockchain__block_records = records
        chain.height_to_hash = hashes.get
        chain.block_store = SimpleNamespace(get_block_record=AsyncMock(return_value=None))
        chain.get_block_records_in_range = AsyncMock(return_value=records)

        async def staking(_farmer, peak, _additional_blocks=None):
            return uint64((int(peak.height) % 7 + 1) * 10**18)

        chain.get_peak_farmer_staking = AsyncMock(side_effect=staking)
        chain.coin_store = SimpleNamespace(get_unspent_amounts_at_heights=AsyncMock(
            side_effect=lambda requests: {
                ph: {height: uint64((int(height) % 7 + 1) * 10**18) for height in heights}
                for ph, heights in requests.items()
            }
        ))
        parent_heights = [24, 27, 30, 33, 36, 39, 42, 45, 48, 51, 54, 57, 60]
        headers = [
            SimpleNamespace(
                height=uint32(parent_height + 1),
                prev_header_hash=hashes[parent_height],
                reward_chain_block=SimpleNamespace(
                    proof_of_space=SimpleNamespace(
                        farmer_public_key=records[hashes[parent_height]].farmer_public_key,
                    )
                ),
            )
            for parent_height in parent_heights
        ]

        headers = headers[::2] + headers[1::2] + headers[:2]
        actual = await chain.get_farmer_difficulty_coeffs(headers)
        chain.get_peak_farmer_staking.assert_not_awaited()
        chain.coin_store.get_unspent_amounts_at_heights.assert_awaited_once()
        expected = [
            await chain.get_farmer_difficulty_coeff(
                header.reward_chain_block.proof_of_space.farmer_public_key,
                uint32(header.height - 1),
                parent_hash=header.prev_header_hash,
            )
            for header in headers
        ]

        self.assertEqual(actual, expected)
        self.assertEqual(chain.get_block_records_in_range.await_count, 1)

    async def test_batch_recent_coefficients_reuses_disk_history(self):
        chain = Blockchain.__new__(Blockchain)
        chain.constants = DEFAULT_CONSTANTS.replace(
            STAKING_ESTIMATE_BLOCK_RANGE=uint32(100),
            BLOCKS_CACHE_SIZE=uint32(40),
            DIFFICULTY_CONSTANT_FACTOR=uint128(2**57),
            NUMBER_ZERO_BITS_PLOT_FILTER=uint8(0),
        )
        chain._peak_height = uint32(1000)
        hashes = {}
        records = {}
        farmers = [AugSchemeMPL.key_gen(bytes([index]) * 32).get_g1() for index in range(1, 5)]
        previous = b"\x00" * 32
        for height in range(1, 1001):
            header_hash = std_hash(f"weight-proof-perf-{height}".encode())
            hashes[height] = header_hash
            records[header_hash] = SimpleNamespace(
                header_hash=header_hash,
                prev_hash=previous,
                height=uint32(height),
                weight=uint128(height * 1000),
                total_iters=uint128(height * 10_000),
                farmer_public_key=farmers[height % len(farmers)],
            )
            previous = header_hash

        chain._Blockchain__block_records = {
            hashes[height]: records[hashes[height]] for height in range(961, 1001)
        }
        chain.height_to_hash = hashes.get
        disk_reads = []

        async def read_record(header_hash):
            disk_reads.append(header_hash)
            return records.get(header_hash)

        async def read_range(start, stop):
            return {hashes[height]: records[hashes[height]] for height in range(start, stop + 1)}

        chain.block_store = SimpleNamespace(get_block_record=AsyncMock(side_effect=read_record))
        chain.get_block_records_in_range = AsyncMock(side_effect=read_range)
        chain.get_peak_farmer_staking = AsyncMock(return_value=uint64(10**18))
        chain.coin_store = SimpleNamespace(get_unspent_amounts_at_heights=AsyncMock(
            side_effect=lambda requests: {
                ph: {height: uint64(10**18) for height in heights}
                for ph, heights in requests.items()
            }
        ))
        headers = [
            SimpleNamespace(
                height=uint32(parent_height + 1),
                prev_header_hash=hashes[parent_height],
                reward_chain_block=SimpleNamespace(
                    proof_of_space=SimpleNamespace(
                        farmer_public_key=records[hashes[parent_height]].farmer_public_key,
                    )
                ),
            )
            for parent_height in range(850, 1000)
        ]

        actual = await chain.get_farmer_difficulty_coeffs(headers)
        batch_disk_reads = len(disk_reads)
        disk_reads.clear()
        expected = [
            await chain.get_farmer_difficulty_coeff(
                header.reward_chain_block.proof_of_space.farmer_public_key,
                uint32(header.height - 1),
                parent_hash=header.prev_header_hash,
            )
            for header in headers
        ]
        scalar_disk_reads = len(disk_reads)

        self.assertEqual(actual, expected)
        self.assertGreater(scalar_disk_reads, batch_disk_reads * 20)

    async def test_batch_recent_coefficients_fall_back_across_a_reorg(self):
        chain = Blockchain.__new__(Blockchain)
        chain.constants = DEFAULT_CONSTANTS.replace(
            STAKING_ESTIMATE_BLOCK_RANGE=uint32(10),
            DIFFICULTY_CONSTANT_FACTOR=uint128(2**57),
            NUMBER_ZERO_BITS_PLOT_FILTER=uint8(0),
        )
        chain._peak_height = uint32(40)
        hashes = {}
        records = {}
        farmers = [bytes([index]) * 32 for index in range(1, 4)]
        previous = b"\x00" * 32
        for height in range(1, 41):
            header_hash = std_hash(f"weight-proof-reorg-{height}".encode())
            hashes[height] = header_hash
            records[header_hash] = SimpleNamespace(
                header_hash=header_hash,
                prev_hash=previous,
                height=uint32(height),
                weight=uint128(height * 1000),
                total_iters=uint128(height * 10_000),
                farmer_public_key=farmers[height % len(farmers)],
            )
            previous = header_hash

        fork_hash = std_hash(b"non-canonical-parent")
        records[fork_hash] = SimpleNamespace(
            header_hash=fork_hash,
            prev_hash=hashes[19],
            height=uint32(20),
            weight=uint128(20_001),
            total_iters=uint128(200_001),
            farmer_public_key=farmers[0],
        )
        chain._Blockchain__block_records = records
        chain.height_to_hash = hashes.get
        chain.block_store = SimpleNamespace(get_block_record=AsyncMock(return_value=None))
        chain.get_block_records_in_range = AsyncMock(return_value=records)
        chain.get_peak_farmer_staking = AsyncMock(return_value=uint64(10**18))
        fork_header = SimpleNamespace(
            height=uint32(21),
            prev_header_hash=fork_hash,
            reward_chain_block=SimpleNamespace(
                proof_of_space=SimpleNamespace(farmer_public_key=farmers[0]),
            ),
        )

        actual = await chain.get_farmer_difficulty_coeffs([fork_header])
        expected = [
            await chain.get_farmer_difficulty_coeff(
                farmers[0], uint32(20), parent_hash=fork_hash,
            )
        ]

        self.assertEqual(actual, expected)
        self.assertEqual(chain.get_block_records_in_range.await_count, 1)


class SyncContext:
    async def __aenter__(self):
        return 0

    async def __aexit__(self, *_args):
        return False


def response(cls, **fields):
    value = cls.__new__(cls)
    for name, content in fields.items():
        object.__setattr__(value, name, content)
    return value


def header(height=1000, weight=10000, header_hash=PEAK_HASH):
    return SimpleNamespace(height=height, weight=weight, header_hash=header_hash)


def peer(name=b"peer", coefficient="1"):
    value = SimpleNamespace(
        peer_node_id=name, closed=False, close=AsyncMock(),
        call_api=AsyncMock(return_value=response(
            RespondBlockHeader, header_block=header(), difficulty_coeff=coefficient,
        )),
        peer_info=SimpleNamespace(host=name.decode()),
    )
    return value


class ProofServerTests(unittest.IsolatedAsyncioTestCase):
    def api(self, generator):
        api = FullNodeAPI.__new__(FullNodeAPI)
        api.log = logging.getLogger("proof-server-test")
        api.full_node = SimpleNamespace(
            weight_proof_handler=SimpleNamespace(get_proof_of_weight=generator),
            blockchain=SimpleNamespace(contains_block=Mock(return_value=True)),
            pow_creation={},
            full_node_store=SimpleNamespace(
                serialized_wp_message_tip=None, serialized_wp_message=None,
            ),
        )
        return api

    async def test_concurrent_callers_share_serialized_proof(self):
        entered, release = asyncio.Event(), asyncio.Event()
        proof = object()

        async def generate(_tip):
            entered.set()
            await release.wait()
            return proof

        api = self.api(AsyncMock(side_effect=generate))
        request = response(RequestProofOfWeight, total_number_of_blocks=1000, tip=b"peak")
        cached_message = Message(uint8(ProtocolMessageTypes.respond_proof_of_weight.value), None, b"proof")
        with patch("chia.full_node.full_node_api.full_node_protocol.RespondProofOfWeight"), patch(
            "chia.full_node.full_node_api.make_msg", return_value=cached_message,
        ) as make_message:
            builder = asyncio.create_task(api.request_proof_of_weight(request))
            await entered.wait()
            waiter = asyncio.create_task(api.request_proof_of_weight(request))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(builder, waiter), 1)
        self.assertEqual(results, [cached_message, cached_message])
        api.full_node.weight_proof_handler.get_proof_of_weight.assert_awaited_once()
        make_message.assert_called_once()

    async def test_serialized_proof_cache_avoids_rebuilding_same_tip(self):
        api = self.api(AsyncMock(side_effect=AssertionError("proof should come from cache")))
        cached_message = object()
        api.full_node.full_node_store.serialized_wp_message_tip = b"peak"
        api.full_node.full_node_store.serialized_wp_message = cached_message
        request = response(RequestProofOfWeight, total_number_of_blocks=1000, tip=b"peak")

        self.assertIs(await api.request_proof_of_weight(request), cached_message)
        api.full_node.weight_proof_handler.get_proof_of_weight.assert_not_awaited()

    async def test_failed_builder_releases_waiter(self):
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = 0

        async def generate(_tip):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("proof build failed")
            return None

        api = self.api(AsyncMock(side_effect=generate))
        request = response(RequestProofOfWeight, total_number_of_blocks=1000, tip=b"peak")
        builder = asyncio.create_task(api.request_proof_of_weight(request))
        await entered.wait()
        waiter = asyncio.create_task(api.request_proof_of_weight(request))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.wait_for(asyncio.gather(builder, waiter), 1), [None, None])
        self.assertTrue(api.full_node.pow_creation[b"peak"].is_set())
        self.assertEqual(attempts, 2)

    async def test_cancelled_builder_releases_waiter(self):
        entered = asyncio.Event()
        attempts = 0

        async def generate(_tip):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                await asyncio.Event().wait()
            return None

        api = self.api(AsyncMock(side_effect=generate))
        request = response(RequestProofOfWeight, total_number_of_blocks=1000, tip=b"peak")
        builder = asyncio.create_task(api.request_proof_of_weight(request))
        await entered.wait()
        waiter = asyncio.create_task(api.request_proof_of_weight(request))
        await asyncio.sleep(0)
        builder.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await builder
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        self.assertTrue(api.full_node.pow_creation[b"peak"].is_set())

    async def test_cache_prunes_completed_events_only(self):
        api = self.api(AsyncMock(return_value=None))
        pending = {bytes([index]): asyncio.Event() for index in range(6)}
        api.full_node.pow_creation.update(pending)
        completed = asyncio.Event()
        completed.set()
        api.full_node.pow_creation[b"old"] = completed
        request = response(RequestProofOfWeight, total_number_of_blocks=1000, tip=b"new")
        await api.request_proof_of_weight(request)
        for key, event in pending.items():
            self.assertIs(api.full_node.pow_creation[key], event)
        self.assertNotIn(b"old", api.full_node.pow_creation)


class FullNodeProofTests(unittest.IsolatedAsyncioTestCase):
    def node(self, peers):
        node = FullNode.__new__(FullNode)
        node.log = logging.getLogger("full-node-proof-test")
        node.config = {}
        node._blockchain = SimpleNamespace(get_peak=Mock(return_value=None))
        node.get_peers_with_peak = Mock(return_value=peers)
        node.in_bad_peak_cache = Mock(return_value=False)
        node.weight_proof_handler = SimpleNamespace(
            validate_weight_proof=AsyncMock(return_value=(True, 50, ["summary"])),
        )
        node.sync_store = SimpleNamespace(set_sync_mode=Mock())
        node._state_changed = Mock()
        return node

    def proof(self, **overrides):
        peak = header(**overrides)
        peak.reward_chain_block = SimpleNamespace(height=peak.height, weight=peak.weight)
        return SimpleNamespace(recent_chain_data=[peak])

    async def test_missing_proof_rotates_without_timed_ban(self):
        bad, good = peer(b"bad"), peer(b"good")
        bad.call_api.return_value = None
        proof = self.proof()
        good.call_api.return_value = response(RespondProofOfWeight, wp=proof, tip=b"peak")
        node = self.node([bad, good])
        with patch("chia.full_node.full_node.random.sample", side_effect=lambda values, _n: values):
            self.assertEqual(await node.request_validate_wp(PEAK_HASH, 1000, 10000), (50, ["summary"]))
        bad.close.assert_awaited_once_with()
        good.close.assert_not_awaited()
        node.weight_proof_handler.validate_weight_proof.assert_awaited_once_with(proof)
        node.sync_store.set_sync_mode.assert_called_once_with(True)

    async def test_invalid_proofs_never_enable_sync(self):
        for kind in ("empty", "height", "weight", "hash", "validation"):
            with self.subTest(kind=kind):
                candidate = peer()
                proof = self.proof()
                if kind == "empty":
                    proof.recent_chain_data = []
                elif kind == "height":
                    proof.recent_chain_data[-1].reward_chain_block.height -= 1
                elif kind == "weight":
                    proof.recent_chain_data[-1].reward_chain_block.weight -= 1
                elif kind == "hash":
                    proof.recent_chain_data[-1].header_hash = b"fork"
                candidate.call_api.return_value = response(RespondProofOfWeight, wp=proof, tip=b"peak")
                node = self.node([candidate])
                if kind == "validation":
                    node.weight_proof_handler.validate_weight_proof.return_value = (False, 0, [])
                with self.assertRaises(RuntimeError):
                    await node.request_validate_wp(PEAK_HASH, 1000, 10000)
                candidate.close.assert_awaited_once_with(600)
                node.sync_store.set_sync_mode.assert_not_called()


class WalletRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def node(self, peers=()):
        node = WalletNode(
            config={}, root_path=Path("."),
            constants=SimpleNamespace(WEIGHT_PROOF_RECENT_BLOCKS=100),
        )
        node.log = logging.getLogger("wallet-recovery-test")
        node._wallet_state_manager = SimpleNamespace(blockchain=SimpleNamespace(
            get_finished_sync_up_to=AsyncMock(return_value=0),
            set_finished_sync_up_to=AsyncMock(),
            get_peak_block=AsyncMock(return_value=None),
            new_valid_weight_proof=AsyncMock(),
            synced_weight_proof=None,
        ))
        node._server = SimpleNamespace(get_connections=Mock(return_value=list(peers)))
        node._new_peak_queue = SimpleNamespace(new_peak_wallet=AsyncMock())
        node.is_trusted = Mock(return_value=False)
        node._weight_proof_handler = SimpleNamespace(validate_weight_proof=AsyncMock(return_value=["record"]))
        return node

    async def test_header_timeout_rearms_retry_without_banning(self):
        candidate = peer()
        candidate.call_api.return_value = None
        node = self.node([candidate])
        node._schedule_untrusted_sync_retry = Mock()
        peak = header()
        await node.new_peak_wallet(peak, candidate)
        node._schedule_untrusted_sync_retry.assert_called_once_with(peak, candidate)
        candidate.close.assert_awaited_once_with()

    async def test_retry_uses_newest_peak_while_delay_is_pending(self):
        candidate = peer()
        node = self.node([candidate])
        old, latest = header(), header(height=1001, weight=10001, header_hash=b"new")
        with patch("chia.wallet.wallet_node.asyncio.sleep", new=AsyncMock()):
            node._schedule_untrusted_sync_retry(old, candidate)
            task = node._untrusted_sync_retry_task
            node._schedule_untrusted_sync_retry(latest, candidate)
            await task
        node.new_peak_queue.new_peak_wallet.assert_awaited_once_with(latest, candidate)

    async def test_incoming_peak_updates_retry_before_serial_queue_is_processed(self):
        candidate = peer()
        node = self.node([candidate])
        api = WalletNodeAPI(node)
        peak = header(height=1001, weight=10001)
        await api.new_peak_wallet(peak, candidate)
        self.assertIs(node._untrusted_sync_peaks[candidate.peer_node_id][0], peak)

    async def test_old_queued_peak_uses_latest_announcement(self):
        candidate = peer()
        candidate.call_api.return_value = None
        node = self.node([candidate])
        old, latest = header(), header(height=1001, weight=10001)
        node._record_untrusted_peak(latest, candidate)
        node._schedule_untrusted_sync_retry = Mock()
        await node.new_peak_wallet(old, candidate)
        self.assertEqual(candidate.call_api.await_args.args[1].height, latest.height)
        node._schedule_untrusted_sync_retry.assert_called_once_with(latest, candidate)

    async def test_disconnect_before_wallet_login_does_not_raise(self):
        candidate = peer()
        node = self.node()
        node._wallet_state_manager = None
        await node.on_disconnect(candidate)

    async def test_status_before_wallet_login_is_not_reported_as_synced(self):
        node = self.node()
        node._wallet_state_manager = None
        api = WalletRpcApi(node)
        self.assertEqual(await api.get_sync_status({}), {
            "synced": False, "syncing": False, "genesis_initialized": False,
        })

    async def test_status_includes_inflight_proof_request(self):
        node = self.node()
        node._weight_proof_syncing = True
        node.wallet_state_manager.sync_mode = False
        node.wallet_state_manager.synced = AsyncMock(return_value=False)
        node.new_peak_queue.has_pending_data_process_items = Mock(return_value=False)
        api = WalletRpcApi(node)
        self.assertEqual(await api.get_sync_status({}), {
            "synced": False, "syncing": True, "genesis_initialized": True,
        })

    async def test_fallback_updates_completion_from_actual_successful_peer(self):
        bad, good = peer(b"bad"), peer(b"good")
        node = self.node([bad, good])
        attempts = []

        async def sync(_syncing, _peak, candidate):
            attempts.append(candidate.peer_node_id)
            if candidate is bad:
                raise RuntimeError("proof temporarily unavailable")
            node.synced_peers.add(candidate.peer_node_id)

        node.long_sync_from_untrusted = AsyncMock(side_effect=sync)
        self.assertTrue(await node.new_peak_from_untrusted(header(), Decimal(1), bad))
        self.assertEqual(attempts, [b"bad", b"good"])
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_awaited_once_with(1000)
        self.assertIn(b"bad", node._untrusted_peer_retry_at)
        bad.close.assert_not_awaited()

    async def test_different_fork_is_not_used_as_fallback(self):
        bad, fork = peer(b"bad"), peer(b"fork")
        fork.call_api.return_value = response(
            RespondBlockHeader, header_block=header(header_hash=b"fork"), difficulty_coeff="1",
        )
        node = self.node([bad, fork])
        node.long_sync_from_untrusted = AsyncMock(side_effect=RuntimeError("unavailable"))
        self.assertFalse(await node.new_peak_from_untrusted(header(), Decimal(1), bad))
        self.assertEqual(node.long_sync_from_untrusted.await_count, 1)
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_not_awaited()

    async def test_fallback_uses_candidate_difficulty_coefficient(self):
        bad, good = peer(b"bad"), peer(b"good", "0.75")
        peak = header(height=20)
        good.call_api.return_value = response(RespondBlockHeader, header_block=peak, difficulty_coeff="0.75")
        node = self.node([bad, good])
        node.sync_from_untrusted_close_to_peak = AsyncMock(side_effect=[False, True])
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal("0.5"), bad))
        self.assertEqual(node.sync_from_untrusted_close_to_peak.await_args_list[-1].args, (
            peak, Decimal("0.75"), good,
        ))

    async def test_wallet_state_sync_prefers_peer_that_supplied_valid_proof(self):
        announced_by, proof_source = peer(b"announced"), peer(b"proof-source")
        node = self.node([announced_by, proof_source])
        peak = header()
        node._weight_proof_sources[peak.header_hash] = proof_source.peer_node_id
        attempts = []

        async def synchronize(_syncing, _peak, candidate):
            attempts.append(candidate.peer_node_id)
            if candidate is proof_source:
                node.synced_peers.add(candidate.peer_node_id)

        node.long_sync_from_untrusted = AsyncMock(side_effect=synchronize)
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), announced_by))
        self.assertEqual(attempts, [proof_source.peer_node_id])
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_awaited_once_with(1000)

    async def test_new_announcer_does_not_delay_catchup_with_subscribed_peer(self):
        announcer, subscribed = peer(b"new"), peer(b"subscribed")
        node = self.node([announcer, subscribed])
        node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 990
        node.synced_peers.add(subscribed.peer_node_id)
        node.sync_from_untrusted_close_to_peak = AsyncMock(return_value=True)
        node.long_sync_from_untrusted = AsyncMock()
        peak = header()
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), announcer))
        node.sync_from_untrusted_close_to_peak.assert_awaited_once_with(peak, Decimal(1), subscribed)
        node.long_sync_from_untrusted.assert_not_awaited()

    async def test_subscribed_peer_on_different_fork_cannot_override_new_announcer(self):
        announcer, subscribed = peer(b"new"), peer(b"subscribed")
        subscribed.call_api.return_value = response(
            RespondBlockHeader, header_block=header(header_hash=b"fork"), difficulty_coeff="1",
        )
        node = self.node([announcer, subscribed])
        node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 990
        node.synced_peers.add(subscribed.peer_node_id)
        node.sync_from_untrusted_close_to_peak = AsyncMock()
        node.long_sync_from_untrusted = AsyncMock()
        peak = header()
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), announcer))
        node.sync_from_untrusted_close_to_peak.assert_not_awaited()
        node.long_sync_from_untrusted.assert_awaited_once_with(False, peak, announcer)

    async def test_first_long_sync_uses_newly_discovered_proof_source(self):
        announcer, source = peer(b"announced"), peer(b"proof-source")
        node = self.node([announcer, source])
        node.wallet_state_manager.set_sync_mode = Mock(return_value=SyncContext())
        peak = header()

        async def fetch(*_args):
            node._weight_proof_sources[peak.header_hash] = source.peer_node_id
            return 0

        async def synchronize(_height, candidate, _fork, **_kwargs):
            node.synced_peers.add(candidate.peer_node_id)

        node.fetch_and_update_weight_proof = AsyncMock(side_effect=fetch)
        node.long_sync = AsyncMock(side_effect=synchronize)
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), announcer))
        node.long_sync.assert_awaited_once_with(1000, source, 0, rollback=True)

    async def test_failed_proof_source_state_sync_reuses_proof_with_other_peer(self):
        fallback, source = peer(b"fallback"), peer(b"proof-source")
        node = self.node([fallback, source])
        node.wallet_state_manager.set_sync_mode = Mock(return_value=SyncContext())
        peak = header()
        node._weight_proof_sources[peak.header_hash] = source.peer_node_id
        node.wallet_state_manager.blockchain.synced_weight_proof = SimpleNamespace(recent_chain_data=[peak])

        async def synchronize(_height, candidate, _fork, **_kwargs):
            if candidate is fallback:
                node.synced_peers.add(candidate.peer_node_id)

        node.long_sync = AsyncMock(side_effect=synchronize)
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=0):
            self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), fallback))
        self.assertEqual([call.args[1] for call in node.long_sync.await_args_list], [source, fallback])
        self.assertIn(source.peer_node_id, node._untrusted_peer_retry_at)
        node._weight_proof_handler.validate_weight_proof.assert_not_awaited()
        source.call_api.assert_awaited_once()
        self.assertIs(source.call_api.await_args.args[0], FullNodeAPI.request_block_header)

    async def test_suppressed_state_sync_error_is_reported_to_peer_fallback(self):
        class SuppressedSyncContext(SyncContext):
            async def __aexit__(self, *_args):
                return True

        candidate = peer()
        node = self.node([candidate])
        node.wallet_state_manager.set_sync_mode = Mock(return_value=SuppressedSyncContext())
        node.fetch_and_update_weight_proof = AsyncMock(return_value=0)
        node.long_sync = AsyncMock(side_effect=ValueError("subscription response was none"))
        with self.assertRaisesRegex(RuntimeError, "wallet state sync did not complete"):
            await node.long_sync_from_untrusted(True, header(), candidate)
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_not_awaited()

    async def test_cooldown_does_not_block_new_peer_for_same_peak(self):
        bad, good = peer(b"bad"), peer(b"good")
        node = self.node([bad])
        node.long_sync_from_untrusted = AsyncMock(side_effect=RuntimeError("unavailable"))
        with patch("chia.wallet.wallet_node.time.monotonic", return_value=100):
            self.assertFalse(await node.new_peak_from_untrusted(header(), Decimal(1), bad))
        node.server.get_connections.return_value = [bad, good]

        async def succeed(_syncing, _peak, candidate):
            self.assertIs(candidate, good)
            node.synced_peers.add(candidate.peer_node_id)

        node.long_sync_from_untrusted.side_effect = succeed
        with patch("chia.wallet.wallet_node.time.monotonic", return_value=101):
            self.assertTrue(await node.new_peak_from_untrusted(header(), Decimal(1), bad))

    async def test_aborted_long_sync_does_not_mark_wallet_synced(self):
        candidate = peer()
        node = self.node([candidate])
        node.long_sync_from_untrusted = AsyncMock()
        self.assertFalse(await node.new_peak_from_untrusted(header(), Decimal(1), candidate))
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_not_awaited()

    async def test_failed_short_sync_falls_back_to_validated_long_sync(self):
        candidate = peer()
        node = self.node([candidate])
        node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 999
        node.synced_peers.add(candidate.peer_node_id)
        node.sync_from_untrusted_close_to_peak = AsyncMock(return_value=False)

        async def succeed(_syncing, _peak, selected):
            node.synced_peers.add(selected.peer_node_id)

        node.long_sync_from_untrusted = AsyncMock(side_effect=succeed)
        self.assertTrue(await node.new_peak_from_untrusted(header(), Decimal(1), candidate))
        self.assertEqual(node.long_sync_from_untrusted.await_count, 1)
        self.assertTrue(node.long_sync_from_untrusted.await_args.args[0])
        self.assertIs(node.long_sync_from_untrusted.await_args.args[2], candidate)

    async def test_retry_is_scheduled_without_another_notification(self):
        candidate = peer()
        node = self.node([candidate])
        peak = response(NewPeakWallet, header_hash=b"peak", height=1000, weight=10000)
        with patch("chia.wallet.wallet_node.asyncio.sleep", new=AsyncMock()) as delay:
            node._schedule_untrusted_sync_retry(peak, candidate)
            first = node._untrusted_sync_retry_task
            node._schedule_untrusted_sync_retry(peak, candidate)
            self.assertIs(node._untrusted_sync_retry_task, first)
            await first
        delay.assert_awaited_once_with(30)
        node.new_peak_queue.new_peak_wallet.assert_awaited_once_with(peak, candidate)

    async def test_retry_selects_live_peer_after_disconnect(self):
        disconnected, live = peer(b"gone"), peer(b"live")
        disconnected.closed = True
        node = self.node([live])
        peak = header()
        with patch("chia.wallet.wallet_node.asyncio.sleep", new=AsyncMock()):
            node._schedule_untrusted_sync_retry(peak, disconnected)
            await node._untrusted_sync_retry_task
        node.new_peak_queue.new_peak_wallet.assert_awaited_once_with(peak, live)

    async def test_retry_stops_on_shutdown_or_when_caught_up(self):
        for state in ("shutdown", "caught_up", "logged_out"):
            with self.subTest(state=state):
                candidate = peer()
                node = self.node([candidate])
                with patch("chia.wallet.wallet_node.asyncio.sleep", new=AsyncMock()):
                    node._schedule_untrusted_sync_retry(header(), candidate)
                    if state == "shutdown":
                        node._shut_down = True
                    elif state == "logged_out":
                        node._wallet_state_manager = None
                    else:
                        node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 1000
                    await node._untrusted_sync_retry_task
                node.new_peak_queue.new_peak_wallet.assert_not_awaited()

    async def test_shutdown_cancels_pending_retry(self):
        candidate = peer()
        node = self.node([candidate])
        node.log_out = Mock()
        node._weight_proof_handler.cancel_weight_proof_tasks = Mock()
        node._schedule_untrusted_sync_retry(header(), candidate)
        task = node._untrusted_sync_retry_task
        node._close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        node.new_peak_queue.new_peak_wallet.assert_not_awaited()

    async def test_closed_peer_peak_is_ignored(self):
        candidate = peer()
        candidate.closed = True
        node = self.node([candidate])
        await node.new_peak_wallet(header(), candidate)
        candidate.call_api.assert_not_awaited()

    async def test_short_sync_rejects_failed_coin_state_validation(self):
        candidate = peer()
        node = self.node([candidate])
        node.wallet_state_manager.lock = asyncio.Lock()
        node.wallet_short_sync_backtrack = AsyncMock(return_value=0)
        node.get_cache_for_peer = Mock()
        node.get_coin_ids_to_subscribe = AsyncMock(return_value=[])
        node.get_puzzle_hashes_to_subscribe = AsyncMock(return_value=[])
        node.add_states_from_peer = AsyncMock(return_value=False)
        with patch("chia.wallet.wallet_node.subscribe_to_phs", new=AsyncMock(return_value=[])):
            with patch("chia.wallet.wallet_node.subscribe_to_coin_updates", new=AsyncMock(return_value=[])):
                self.assertFalse(await node.sync_from_untrusted_close_to_peak(header(), Decimal(1), candidate))
        self.assertNotIn(candidate.peer_node_id, node.synced_peers)

    async def test_duplicate_completed_peak_applies_late_race_state_without_another_proof(self):
        candidate = peer()
        node = self.node([candidate])
        peak, late_state = header(), object()
        node.synced_peers.add(candidate.peer_node_id)
        node.wallet_state_manager.lock = asyncio.Lock()
        node.wallet_state_manager.state_changed = Mock()
        node.wallet_state_manager.blockchain.get_peak_block.return_value = peak
        node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 1000
        cache = Mock()
        cache.get_race_cache.return_value = {late_state}
        node.get_cache_for_peer = Mock(return_value=cache)
        node.add_states_from_peer = AsyncMock(return_value=True)
        node.fetch_and_update_weight_proof = AsyncMock()
        self.assertTrue(await node.new_peak_from_untrusted(peak, Decimal(1), candidate))
        node.add_states_from_peer.assert_awaited_once_with([late_state], candidate)
        node.fetch_and_update_weight_proof.assert_not_awaited()
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_awaited_once_with(1000)

    async def test_duplicate_incomplete_peak_replays_unfinished_race_states_before_completion(self):
        for valid in (True, False):
            with self.subTest(valid_states=valid):
                candidate = peer()
                node = self.node([candidate])
                peak = header()
                node.synced_peers.add(candidate.peer_node_id)
                node.wallet_state_manager.lock = asyncio.Lock()
                node.wallet_state_manager.state_changed = Mock()
                node.wallet_state_manager.blockchain.get_peak_block.return_value = peak
                node.wallet_state_manager.blockchain.get_finished_sync_up_to.return_value = 997
                cache = Mock()
                states = {height: {object()} for height in (998, 999, 1000)}
                cache.get_race_cache.side_effect = states.__getitem__
                node.get_cache_for_peer = Mock(return_value=cache)
                node.add_states_from_peer = AsyncMock(return_value=valid)
                node.fetch_and_update_weight_proof = AsyncMock()
                self.assertEqual(await node.new_peak_from_untrusted(peak, Decimal(1), candidate), valid)
                if valid:
                    self.assertEqual(node.add_states_from_peer.await_count, 3)
                    self.assertEqual(
                        [call.args[0] for call in node.add_states_from_peer.await_args_list],
                        [list(states[height]) for height in (998, 999, 1000)],
                    )
                    node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_awaited_once_with(1000)
                else:
                    node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_not_awaited()
                    cache.cleanup_race_cache.assert_not_called()
                node.fetch_and_update_weight_proof.assert_not_awaited()

    async def test_equal_weight_different_peak_is_not_treated_as_completed(self):
        candidate = peer()
        node = self.node([candidate])
        node.synced_peers.add(candidate.peer_node_id)
        node.wallet_state_manager.lock = asyncio.Lock()
        node.wallet_state_manager.blockchain.get_peak_block.return_value = header()
        node.get_cache_for_peer = Mock()
        self.assertFalse(await node.sync_from_untrusted_close_to_peak(
            header(header_hash=b"other-fork"), Decimal(1), candidate,
        ))
        node.get_cache_for_peer.assert_not_called()
        node.wallet_state_manager.blockchain.set_finished_sync_up_to.assert_not_awaited()

    async def test_missing_proof_is_retryable_and_is_not_stored(self):
        candidate = peer()
        candidate.call_api.return_value = None
        node = self.node([candidate])
        with self.assertRaisesRegex(RuntimeError, "response was none"):
            await node.fetch_and_update_weight_proof(candidate, header())
        candidate.close.assert_not_awaited()
        node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_not_awaited()

    async def test_invalid_proof_is_banned_and_never_stored(self):
        for kind in ("empty", "height", "weight", "hash", "validation"):
            with self.subTest(kind=kind):
                candidate = peer()
                proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])
                if kind == "empty":
                    proof.recent_chain_data = []
                elif kind == "height":
                    proof.recent_chain_data[-1].height -= 1
                elif kind == "weight":
                    proof.recent_chain_data[-1].weight -= 1
                elif kind == "hash":
                    proof.recent_chain_data[-1].header_hash = b"fork"
                candidate.call_api.return_value = response(RespondProofOfWeight, wp=proof, tip=b"peak")
                node = self.node([candidate])
                if kind == "validation":
                    node._weight_proof_handler.validate_weight_proof.side_effect = ValueError("invalid proof")
                with self.assertRaises(ValueError):
                    await node.fetch_and_update_weight_proof(candidate, header())
                candidate.close.assert_awaited_once_with(600)
                node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_not_awaited()

    async def test_valid_proof_is_validated_before_storage(self):
        candidate = peer()
        proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])
        candidate.call_api.return_value = response(RespondProofOfWeight, wp=proof, tip=b"peak")
        node = self.node([candidate])
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=50):
            self.assertEqual(await node.fetch_and_update_weight_proof(candidate, header()), 50)
        node._weight_proof_handler.validate_weight_proof.assert_awaited_once_with(proof, False, None)
        node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_awaited_once_with(proof, ["record"])

    async def test_slow_proof_peer_does_not_delay_a_healthy_peer(self):
        slow, good = peer(b"slow"), peer(b"good")
        slow_started, slow_cancelled = asyncio.Event(), asyncio.Event()
        proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])

        async def wait_for_timeout(*_args, **_kwargs):
            slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                slow_cancelled.set()

        async def respond(method, *_args, **_kwargs):
            if method is FullNodeAPI.request_block_header:
                return response(RespondBlockHeader, header_block=header(), difficulty_coeff="1")
            await slow_started.wait()
            return response(RespondProofOfWeight, wp=proof, tip=b"peak")

        slow.call_api.side_effect = wait_for_timeout
        good.call_api.side_effect = respond
        node = self.node([slow, good])
        node.config["weight_proof_peer_delay"] = 0.01
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=50):
            self.assertEqual(await asyncio.wait_for(node.fetch_and_update_weight_proof(slow, header()), 1), 50)
        self.assertTrue(slow_cancelled.is_set())
        node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_awaited_once_with(proof, ["record"])
        self.assertFalse(node._weight_proof_syncing)

    async def test_peer_connected_after_proof_started_is_considered(self):
        slow, good = peer(b"slow"), peer(b"new")
        started = asyncio.Event()
        proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])

        async def unavailable(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        async def reply(method, *_args, **_kwargs):
            if method is FullNodeAPI.request_block_header:
                return response(RespondBlockHeader, header_block=header(), difficulty_coeff="1")
            return response(RespondProofOfWeight, wp=proof, tip=b"peak")

        slow.call_api.side_effect = unavailable
        good.call_api.side_effect = reply
        node = self.node([slow])
        node.config["weight_proof_peer_delay"] = 0.01
        task = asyncio.create_task(node.fetch_and_update_weight_proof(slow, header()))
        await started.wait()
        node.server.get_connections.return_value = [slow, good]
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=50):
            self.assertEqual(await asyncio.wait_for(task, 1), 50)

    async def test_stored_valid_proof_is_reused_after_state_subscription_failure(self):
        candidate = peer()
        node = self.node([candidate])
        proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])
        node.wallet_state_manager.blockchain.synced_weight_proof = proof
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=50):
            self.assertEqual(await node.fetch_and_update_weight_proof(candidate, header()), 50)
        candidate.call_api.assert_not_awaited()
        node._weight_proof_handler.validate_weight_proof.assert_not_awaited()

    async def test_wrong_proof_does_not_hide_healthy_peer(self):
        bad, good = peer(b"bad"), peer(b"good")
        bad.call_api.return_value = response(
            RespondProofOfWeight, wp=SimpleNamespace(sub_epochs=[], recent_chain_data=[header(header_hash=b"fork")]), tip=b"fork",
        )
        proof = SimpleNamespace(sub_epochs=[], recent_chain_data=[header()])

        async def reply(method, *_args, **_kwargs):
            if method is FullNodeAPI.request_block_header:
                return response(RespondBlockHeader, header_block=header(), difficulty_coeff="1")
            return response(RespondProofOfWeight, wp=proof, tip=b"peak")

        good.call_api.side_effect = reply
        node = self.node([bad, good])
        with patch("chia.wallet.wallet_node.get_wp_fork_point", return_value=50):
            self.assertEqual(await node.fetch_and_update_weight_proof(bad, header()), 50)
        bad.close.assert_awaited_once_with(600)
        node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_awaited_once_with(proof, ["record"])

    async def test_proof_from_different_fork_is_not_requested(self):
        bad, fork = peer(b"bad"), peer(b"fork")
        bad.call_api.return_value = None
        fork.call_api.return_value = response(
            RespondBlockHeader, header_block=header(header_hash=b"fork"), difficulty_coeff="1",
        )
        node = self.node([bad, fork])
        with self.assertRaises(RuntimeError):
            await node.fetch_and_update_weight_proof(bad, header())
        self.assertEqual(fork.call_api.await_count, 1)
        node._weight_proof_handler.validate_weight_proof.assert_not_awaited()
        node.wallet_state_manager.blockchain.new_valid_weight_proof.assert_not_awaited()


class ProofResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_validation_keeps_rpc_and_network_loop_responsive(self):
        handler = WalletWeightProofHandler.__new__(WalletWeightProofHandler)
        handler._constants = DEFAULT_CONSTANTS
        entered, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        observed_threads = []

        def summaries(*args):
            observed_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise AssertionError("the event loop was blocked by summary validation")
            return None, None

        with patch("chia.wallet.wallet_weight_proof_handler._validate_sub_epoch_summaries", side_effect=summaries):
            task = asyncio.create_task(handler.validate_weight_proof(object()))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertNotEqual(observed_threads, [main_thread])
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "sub epoch data validation"):
                await task

    async def test_invalid_summary_never_runs_or_skips_other_validation(self):
        handler = WalletWeightProofHandler.__new__(WalletWeightProofHandler)
        handler._constants = DEFAULT_CONSTANTS
        with patch(
            "chia.wallet.wallet_weight_proof_handler._validate_sub_epoch_summaries",
            return_value=(None, None),
        ), patch("chia.wallet.wallet_weight_proof_handler.validate_weight_proof_inner", new_callable=AsyncMock) as inner:
            with self.assertRaisesRegex(ValueError, "sub epoch data validation"):
                await handler.validate_weight_proof(object())
            inner.assert_not_awaited()


class PendingRequestTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        return WSGoldConnection(
            ws=SimpleNamespace(closed=False, close=AsyncMock(), send_bytes=AsyncMock()),
            api=Mock(), local_type=NodeType.WALLET, local_port=None,
            local_capabilities_for_handshake=[], local_capabilities=[],
            peer_info=PeerInfo("127.0.0.1", uint16(14444)),
            peer_node_id=std_hash(b"transport-peer"),
            log=logging.getLogger("transport-recovery-test"),
            close_callback=AsyncMock(), outbound_rate_limiter=Mock(
                process_msg_and_check=Mock(return_value=True),
            ),
            inbound_rate_limiter=Mock(), is_outbound=True,
            received_message_callback=None,
        )

    async def test_broken_writer_closes_connection_and_releases_request(self):
        connection = self.connection()
        connection.ws.send_bytes.side_effect = BrokenPipeError("test broken pipe")
        request = Message(uint8(ProtocolMessageTypes.request_proof_of_weight.value), None, b"request")
        waiter = asyncio.create_task(connection.send_request(request, 360))
        connection.outbound_task = asyncio.create_task(connection.outbound_handler())
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        await asyncio.wait_for(connection.outbound_task, 1)
        self.assertTrue(connection.closed)
        self.assertTrue(connection._close_event.is_set())
        self.assertFalse(connection.outbound_task.cancelled())
        self.assertEqual(connection.pending_requests, {})
        connection.close_callback.assert_awaited_once_with(connection, 0, closed_connection=False)

    async def test_broken_reader_closes_connection_and_releases_request(self):
        connection = self.connection()
        connection._read_one_message = AsyncMock(side_effect=ConnectionResetError("test reset"))
        waiter = asyncio.create_task(connection.send_request(Message(uint8(1), None, b"request"), 360))
        await connection.outgoing_queue.get()
        connection.inbound_task = asyncio.create_task(connection.inbound_handler())
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        await asyncio.wait_for(connection.inbound_task, 1)
        self.assertTrue(connection.closed)
        self.assertFalse(connection.inbound_task.cancelled())
        connection.close_callback.assert_awaited_once_with(connection, 0, closed_connection=False)

    async def test_socket_close_failure_still_releases_requests_and_session(self):
        connection = self.connection()
        connection.ws.close.side_effect = OSError("test close error")
        connection.session = SimpleNamespace(close=AsyncMock())
        waiter = asyncio.create_task(connection.send_request(Message(uint8(1), None, b"request"), 360))
        await connection.outgoing_queue.get()
        with self.assertRaisesRegex(OSError, "test close error"):
            await connection.close()
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        self.assertEqual(connection.pending_requests, {})
        self.assertTrue(connection._close_event.is_set())
        connection.session.close.assert_awaited_once()
        connection.close_callback.assert_awaited_once_with(connection, 0, closed_connection=False)

    async def test_stalled_socket_close_does_not_stall_pending_requests(self):
        connection = self.connection()
        release = asyncio.Event()

        async def close_socket(**kwargs):
            await release.wait()
        connection.ws.close.side_effect = close_socket
        waiter = asyncio.create_task(connection.send_request(Message(uint8(1), None, b"request"), 360))
        await connection.outgoing_queue.get()
        closing = asyncio.create_task(connection.close())
        try:
            self.assertIsNone(await asyncio.wait_for(waiter, 1))
            self.assertFalse(closing.done())
        finally:
            release.set()
            await asyncio.wait_for(closing, 1)

    async def test_closing_from_api_task_does_not_cancel_itself(self):
        connection = self.connection()
        connection.api_tasks[std_hash(b"current-task")] = asyncio.current_task()
        await connection.close()
        # Deliver any cancellation queued by an accidental self-cancel.
        await asyncio.sleep(0)
        self.assertTrue(connection._close_event.is_set())
        connection.close_callback.assert_awaited_once()

    async def test_weight_proof_send_failure_is_not_logged_as_success(self):
        connection = self.connection()
        connection.log = Mock()
        connection.ws.send_bytes.side_effect = BrokenPipeError("test proof send")
        message = Message(uint8(ProtocolMessageTypes.respond_proof_of_weight.value), uint16(2), b"proof")
        with self.assertRaises(BrokenPipeError):
            await connection._send_message(message)
        self.assertEqual(connection.bytes_written, 0)
        self.assertTrue(any("Weight proof send failed" in str(call) for call in connection.log.warning.call_args_list))
        self.assertFalse(any("Weight proof sent:" in str(call) for call in connection.log.info.call_args_list))

    async def test_cancelled_request_removes_pending_transport_state(self):
        connection = WSGoldConnection.__new__(WSGoldConnection)
        connection.closed = False
        connection.is_outbound = True
        connection.request_nonce = uint16(0)
        connection.pending_requests = {}
        connection.request_results = {}
        connection.outgoing_queue = asyncio.Queue()
        connection.log = logging.getLogger("pending-request-test")
        request = Message(uint8(1), None, b"request")
        task = asyncio.create_task(connection.send_request(request, 360))
        await connection.outgoing_queue.get()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(connection.pending_requests, {})
        self.assertEqual(connection.request_results, {})


if __name__ == "__main__":
    unittest.main()
