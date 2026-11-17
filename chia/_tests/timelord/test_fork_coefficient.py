"""Branch context regressions with SQLite coin state and native header validation."""

from __future__ import annotations

from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gold_rs import AugSchemeMPL

from chia._tests.timelord.test_iteration_consistency import CONSTANTS, PK, QUALITY, SK
from chia._tests.timelord import test_candidate_refresh as candidate_fixture
from chia._tests.timelord.fork_coefficient_baseline import LegacyBlockchain
from chia.consensus import blockchain as blockchain_module
from chia.consensus import block_header_validation as header_validation
from chia.consensus import multiprocess_validation
from chia.consensus.blockchain import AddBlockResult, Blockchain
from chia.consensus.coinbase import create_puzzlehash_for_pk
from chia.full_node.block_store import BlockStore
from chia.full_node.coin_store import CoinStore
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_record import CoinRecord
from chia.util.db_wrapper import DBWrapper2
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64, uint128


def legacy_class():
    return LegacyBlockchain


class BranchCoefficientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.constants = CONSTANTS.replace(STAKING_ESTIMATE_BLOCK_RANGE=uint32(3))
        self.ph = create_puzzlehash_for_pk(PK)
        other_pk = AugSchemeMPL.key_gen(std_hash(b"other-farmer")).get_g1()
        self.records = {}
        self.maps = {"a": {}, "b": {}}
        self.bodies = {}
        self.seed = Coin(std_hash(b"seed"), self.ph, uint64(10**12))
        self.rewards = {}
        common = self.constants.GENESIS_CHALLENGE
        for height in range(3):
            record = self.record("common", height, common, height * 1000, PK)
            self.maps["a"][height] = self.maps["b"][height] = record.header_hash
            common = record.header_hash
        for branch in ("a", "b"):
            previous = common
            self.rewards[branch] = {}
            for height in range(3, 7):
                record = self.record(
                    branch, height, previous, height * (1000 if branch == "a" else 1600),
                    other_pk if branch == "a" and height == 4 else PK,
                )
                self.maps[branch][height] = record.header_hash
                reward = Coin(
                    std_hash(f"{branch}-reward-{height}".encode()), self.ph,
                    uint64((height + (0 if branch == "a" else 3)) * 10**12),
                )
                self.rewards[branch][height] = reward
                spend = (branch == "a" and height == 3) or (branch == "b" and height == 4)
                npc = SimpleNamespace(error=None, conds=SimpleNamespace(spends=[
                    SimpleNamespace(coin_id=self.seed.name(), puzzle_hash=self.ph, create_coin=[])
                ] if spend else []))
                body = SimpleNamespace(
                    height=uint32(height), header_hash=record.header_hash, prev_header_hash=previous,
                    transactions_generator=object() if spend else None,
                    transactions_info=SimpleNamespace(cost=0),
                    foliage_transaction_block=SimpleNamespace(timestamp=uint64(height)),
                    get_included_reward_coins=lambda coin=reward: [coin],
                    npc=npc,
                )
                self.bodies[record.header_hash] = body
                previous = record.header_hash
        self.chains = {}
        for branch in ("a", "b"):
            database = await self.stack.enter_async_context(
                DBWrapper2.managed(Path(directory) / f"{branch}.sqlite", db_version=2, reader_count=1)
            )
            store = await CoinStore.create(database)
            await store._add_coin_records([
                CoinRecord(self.seed, uint32(1), uint32(3 if branch == "a" else 4), False, uint64(1)),
                *[CoinRecord(coin, uint32(height), uint32(0), True, uint64(height))
                  for height, coin in self.rewards[branch].items()],
            ])
            chain = Blockchain.__new__(Blockchain)
            chain.constants = self.constants
            chain._peak_height = uint32(6)
            chain._Blockchain__block_records = dict(self.records)
            chain.height_to_hash = self.maps[branch].get
            chain.coin_store = store
            chain.block_store = SimpleNamespace(
                get_block_record=AsyncMock(side_effect=self.records.get),
                get_full_block=AsyncMock(side_effect=self.bodies.get),
            )
            chain.get_block_generator = AsyncMock(
                side_effect=lambda block, additional: SimpleNamespace(npc=block.npc)
            )
            self.chains[branch] = chain
        self.generator = patch.object(
            blockchain_module, "get_name_puzzle_conditions", side_effect=lambda generator, *args, **kwargs: generator.npc
        )
        self.generator.start()
        self.stack.callback(self.generator.stop)

    def record(self, branch, height, previous, total_iters, farmer):
        record = SimpleNamespace(
            header_hash=std_hash(f"{branch}-{height}".encode()), height=uint32(height), prev_hash=previous,
            weight=uint128((height + 1) * 7), total_iters=uint128(total_iters),
            farmer_public_key=farmer,
        )
        self.records[record.header_hash] = record
        return record

    async def asyncTearDown(self):
        await self.stack.aclose()

    async def coefficient(self, chain, branch="b", height=6, **kwargs):
        return await chain.get_farmer_difficulty_coeff(
            PK, uint32(height), parent_hash=self.maps[branch][height], **kwargs
        )

    async def test_original_height_lookup_diverges_but_parent_hash_converges(self):
        legacy = legacy_class()
        old_a, old_b = legacy.__new__(legacy), legacy.__new__(legacy)
        old_a.__dict__.update(self.chains["a"].__dict__)
        old_b.__dict__.update(self.chains["b"].__dict__)
        self.assertNotEqual(
            await old_a.get_farmer_difficulty_coeff(PK, uint32(6)),
            await old_b.get_farmer_difficulty_coeff(PK, uint32(6)),
        )
        self.assertEqual(
            await self.coefficient(self.chains["a"]), await self.coefficient(self.chains["b"])
        )

    async def test_canonical_formula_is_unchanged(self):
        legacy = legacy_class()
        for branch, chain in self.chains.items():
            old = legacy.__new__(legacy)
            old.__dict__.update(chain.__dict__)
            self.assertEqual(
                await old.get_farmer_difficulty_coeff(PK, uint32(6)),
                await self.coefficient(chain, branch),
            )

    async def test_network_space_uses_ancestor_on_requested_branch(self):
        parent = self.records[self.maps["b"][6]]
        self.assertEqual(
            await self.chains["a"].get_peak_network_space(3, parent),
            await self.chains["b"].get_peak_network_space(3, parent),
        )

    async def test_fork_stake_replays_spends_rewards_and_excludes_parent_height(self):
        a, b = self.chains["a"], self.chains["b"]
        before = await a.coin_store.get_all_coins(True)
        parent = self.records[self.maps["b"][6]]
        actual = await a.get_peak_farmer_staking(PK, parent)
        expected = sum(coin.amount for height, coin in self.rewards["b"].items() if height < 6)
        self.assertEqual(actual, expected)
        self.assertEqual(actual, await b.get_peak_farmer_staking(PK, parent))
        self.assertEqual(set(before), set(await a.coin_store.get_all_coins(True)))

    async def test_cold_block_cache_does_not_truncate_farmer_history(self):
        chain = self.chains["a"]
        expected = await self.coefficient(chain)
        chain._Blockchain__block_records.clear()
        self.assertEqual(await self.coefficient(chain), expected)

    async def test_branch_parent_can_be_above_local_peak(self):
        chain = self.chains["a"]
        chain.height_to_hash = {h: v for h, v in self.maps["a"].items() if h <= 4}.get
        chain._peak_height = uint32(4)
        self.assertEqual(await self.coefficient(chain), await self.coefficient(self.chains["b"]))

    async def test_missing_parent_is_not_replaced_by_main_chain_height(self):
        with self.assertRaisesRegex(ValueError, "Missing coefficient parent"):
            await self.chains["a"].get_farmer_difficulty_coeff(
                PK, uint32(6), parent_hash=std_hash(b"missing-parent")
            )

    async def test_parent_height_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "height does not match"):
            await self.chains["a"].get_farmer_difficulty_coeff(
                PK, uint32(5), parent_hash=self.maps["b"][6]
            )

    async def test_missing_history_is_not_counted_as_zero(self):
        del self.records[self.maps["b"][4]]
        self.chains["a"]._Blockchain__block_records.clear()
        with self.assertRaisesRegex(ValueError, "Missing coefficient history"):
            await self.coefficient(self.chains["a"])

    async def test_pending_batch_bodies_can_supply_fork_coin_state(self):
        expected = await self.coefficient(self.chains["b"])
        pending = {key: value for key, value in self.bodies.items() if key in self.maps["b"].values()}
        for key in pending:
            del self.bodies[key]
        self.assertEqual(await self.coefficient(self.chains["a"], additional_blocks=pending), expected)
        with self.assertRaisesRegex(ValueError, "Missing coefficient fork block"):
            await self.coefficient(self.chains["a"])


class ForkAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_header_batch_reorg_and_restart_persist_same_peak(self):
        fixture = candidate_fixture.CandidateRefreshTest()
        fixture.setUp()
        child = fixture.finish(fixture.original)
        data = fixture.parent_block.foliage.foliage_block_data.replace(extension_data=std_hash(b"competing-block"))
        sibling = fixture.parent_block.replace(foliage=fixture.parent_block.foliage.replace(
            foliage_block_data=data,
            foliage_block_data_signature=AugSchemeMPL.sign(SK, data.get_hash()),
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with DBWrapper2.managed(root / "chain.sqlite", db_version=2, reader_count=1) as database:
                coin_store = await CoinStore.create(database)
                block_store = await BlockStore.create(database)
                chain = await Blockchain.create(coin_store, block_store, CONSTANTS, root, 0, single_threaded=True)
                try:
                    with patch.object(header_validation, "verify_and_get_quality_string", return_value=QUALITY), patch.object(
                        multiprocess_validation, "verify_and_get_quality_string", return_value=QUALITY
                    ):
                        batch = [fixture.genesis, fixture.parent_block, child]
                        results = await chain.pre_validate_blocks_multiprocessing(batch, {}, validate_signatures=True)
                        self.assertEqual(len(results), 3)
                        self.assertTrue(all(result.error is None for result in results), results)
                        self.assertFalse(any(chain.contains_block(block.header_hash) for block in batch))
                        for block in [fixture.genesis, sibling, fixture.parent_block, child]:
                            result = (await chain.pre_validate_blocks_multiprocessing(
                                [block], {}, validate_signatures=True
                            ))[0]
                            self.assertIsNone(result.error)
                            status, error, change = await chain.add_block(block, result, None)
                            self.assertIsNone(error)
                            if block == fixture.parent_block:
                                self.assertEqual(status, AddBlockResult.ADDED_AS_ORPHAN)
                                self.assertEqual(chain.get_peak().header_hash, sibling.header_hash)
                            else:
                                self.assertEqual(status, AddBlockResult.NEW_PEAK)
                            if block == child:
                                self.assertEqual(change.fork_height, 0)
                    self.assertEqual(chain.get_peak().header_hash, child.header_hash)
                finally:
                    chain.shut_down()
                restored = await Blockchain.create(coin_store, block_store, CONSTANTS, root, 0, single_threaded=True)
                try:
                    self.assertEqual(restored.get_peak().header_hash, child.header_hash)
                    self.assertEqual(restored.height_to_hash(uint32(1)), fixture.parent_block.header_hash)
                finally:
                    restored.shut_down()


if __name__ == "__main__":
    unittest.main(verbosity=2)
