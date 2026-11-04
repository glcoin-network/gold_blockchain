"""Parent advancement changes stake difficulty after the candidate has been signed."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from decimal import Decimal
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gold_rs import AugSchemeMPL, G2Element

from chia._tests.timelord.test_iteration_consistency import (
    CONSTANTS, PK, QUALITY, SK, make_genesis, proof_of_space, sign,
)
from chia.consensus import block_header_validation as validation
from chia.consensus.block_creation import create_unfinished_block, unfinished_block_to_full_block
from chia.consensus.full_block_to_block_record import block_to_block_record
from chia.consensus.blockchain import AddBlockResult, Blockchain
from chia.consensus.multiprocess_validation import PreValidationResult
from chia.consensus.pot_iterations import calculate_ip_iters, calculate_iterations_quality, calculate_sp_iters
from chia.full_node.candidate_refresh import refresh_candidate_iterations
from chia.full_node.full_node_store import FullNodeStore
from chia.full_node.full_node import FullNode
from chia.full_node.full_node_api import FullNodeAPI
from chia.full_node.block_store import BlockStore
from chia.full_node.coin_store import CoinStore
from chia.full_node.signage_point import SignagePoint
from chia.protocols import farmer_protocol, timelord_protocol
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.server.outbound_message import NodeType
from chia.timelord.timelord_api import TimelordAPI
from chia.timelord.types import Chain
from chia.types.blockchain_format.pool_target import PoolTarget
from chia.util.block_cache import BlockCache
from chia.util.errors import Err
from chia.util.generator_tools import get_block_header
from chia.util.ints import uint8, uint32, uint64, uint128
from chia.util.vdf_prover import get_vdf_info_and_proof
from chia.util.hash import std_hash
from chia.util.lru_cache import LRUCache
from chia.util.db_wrapper import DBWrapper2
from chia._tests.timelord import test_iteration_consistency as iteration_fixture
from chia.types.blockchain_format.vdf import VDFInfo, VDFProof
from chia.types.blockchain_format.classgroup import ClassgroupElement
from chia.types.blockchain_format.slots import ChallengeChainSubSlot, RewardChainSubSlot, SubSlotProofs
from chia.types.end_of_slot_bundle import EndOfSubSlotBundle
from chia.types.blockchain_format.sized_bytes import bytes32


class CandidateRefreshTest(unittest.TestCase):
    def setUp(self):
        genesis, record = make_genesis()
        self.records = {record.header_hash: record}
        self.cache = BlockCache(self.records)
        self.store = FullNodeStore(CONSTANTS)
        self.genesis = genesis
        self.old_parent = record
        self.index = uint8(16)
        self.sp_iters = calculate_sp_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, self.index)
        cc, cc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output, CONSTANTS.GENESIS_CHALLENGE, self.sp_iters - record.total_iters,
        )
        rc, rc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output.get_default_element(),
            record.reward_infusion_new_challenge, self.sp_iters - record.total_iters,
        )
        self.signage = SignagePoint(cc.replace(number_of_iterations=self.sp_iters), cc_proof, rc, rc_proof)
        self.quality = QUALITY
        self.original = self.candidate(Decimal(20))

        # A competing SP=14 infuses after SP=16, before our candidate's IP.
        parent_index = uint8(14)
        parent_sp_iters = calculate_sp_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, parent_index)
        parent_sp_cc, parent_sp_cc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output, CONSTANTS.GENESIS_CHALLENGE,
            parent_sp_iters - record.total_iters,
        )
        parent_sp_rc, parent_sp_rc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output.get_default_element(), record.reward_infusion_new_challenge,
            parent_sp_iters - record.total_iters,
        )
        parent_required = calculate_iterations_quality(
            CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, QUALITY, 32, CONSTANTS.DIFFICULTY_STARTING,
            Decimal(20), parent_sp_cc.output.get_hash(),
        )
        parent_iters = calculate_ip_iters(
            CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, parent_index, parent_required,
        )
        parent_unfinished = create_unfinished_block(
            CONSTANTS, uint128(0), CONSTANTS.SUB_SLOT_ITERS_STARTING, parent_index, parent_sp_iters, parent_iters,
            proof_of_space(), CONSTANTS.GENESIS_CHALLENGE, genesis.foliage.foliage_block_data.farmer_reward_puzzle_hash,
            PoolTarget(CONSTANTS.GENESIS_PRE_FARM_POOL_PUZZLE_HASH, uint32(0)), sign,
            lambda target, _pk: AugSchemeMPL.sign(SK, bytes(target)),
            SignagePoint(parent_sp_cc.replace(number_of_iterations=parent_sp_iters), parent_sp_cc_proof,
                         parent_sp_rc, parent_sp_rc_proof),
            uint64(2), self.cache, prev_block=record,
        )
        parent_cc, _ = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output, CONSTANTS.GENESIS_CHALLENGE, parent_iters - record.total_iters,
        )
        parent_rc, _ = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output.get_default_element(),
            record.reward_infusion_new_challenge, parent_iters - record.total_iters,
        )
        from chia.types.blockchain_format.slots import ChallengeBlockInfo
        self.icc_challenge = ChallengeBlockInfo(
            genesis.reward_chain_block.proof_of_space, genesis.reward_chain_block.challenge_chain_sp_vdf,
            genesis.reward_chain_block.challenge_chain_sp_signature, genesis.reward_chain_block.challenge_chain_ip_vdf,
        ).get_hash()
        parent_icc, parent_icc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output.get_default_element(), self.icc_challenge,
            parent_iters - record.total_iters,
        )
        _, parent_cc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output, CONSTANTS.GENESIS_CHALLENGE, parent_iters - record.total_iters,
        )
        _, parent_rc_proof = get_vdf_info_and_proof(
            CONSTANTS, record.challenge_vdf_output.get_default_element(),
            record.reward_infusion_new_challenge, parent_iters - record.total_iters,
        )
        self.parent_block = unfinished_block_to_full_block(
            parent_unfinished, parent_cc.replace(number_of_iterations=parent_iters), parent_cc_proof,
            parent_rc, parent_rc_proof, parent_icc, parent_icc_proof, [],
            record, self.cache, parent_sp_iters, CONSTANTS.DIFFICULTY_STARTING,
        )
        self.parent = block_to_block_record(CONSTANTS, self.cache, parent_required, self.parent_block, None)
        self.records[self.parent.header_hash] = self.parent

    def candidate(self, coefficient):
        required = calculate_iterations_quality(
            CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, self.quality, 32,
            CONSTANTS.DIFFICULTY_STARTING, coefficient, self.signage.cc_vdf.output.get_hash(),
        )
        ip = calculate_ip_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, self.index, required)
        return create_unfinished_block(
            CONSTANTS, uint128(0), CONSTANTS.SUB_SLOT_ITERS_STARTING, self.index, self.sp_iters, ip,
            proof_of_space(), CONSTANTS.GENESIS_CHALLENGE, self.genesis.foliage.foliage_block_data.farmer_reward_puzzle_hash,
            PoolTarget(CONSTANTS.GENESIS_PRE_FARM_POOL_PUZZLE_HASH, uint32(0)),
            sign, lambda target, _pk: AugSchemeMPL.sign(SK, bytes(target)), self.signage,
            uint64(2), self.cache, prev_block=self.old_parent,
        )

    def refresh(self, coefficient=Decimal(10), parent=None):
        with patch("chia.full_node.candidate_refresh.verify_and_get_quality_string", return_value=self.quality):
            return refresh_candidate_iterations(
                CONSTANTS, self.cache, self.store, self.original, parent or self.parent, coefficient
            )

    def finish(self, candidate):
        delta = candidate.total_iters - self.parent.total_iters
        cc, cc_proof = get_vdf_info_and_proof(
            CONSTANTS, self.parent.challenge_vdf_output, CONSTANTS.GENESIS_CHALLENGE, delta,
        )
        cc = cc.replace(number_of_iterations=uint64(candidate.total_iters))
        rc, rc_proof = get_vdf_info_and_proof(
            CONSTANTS, self.parent.challenge_vdf_output.get_default_element(),
            self.parent.reward_infusion_new_challenge, delta,
        )
        icc, icc_proof = get_vdf_info_and_proof(
            CONSTANTS, self.parent.infused_challenge_vdf_output, self.icc_challenge, delta,
        )
        return unfinished_block_to_full_block(
            candidate, cc, cc_proof, rc, rc_proof, icc, icc_proof, [],
            self.parent, self.cache, self.sp_iters, CONSTANTS.DIFFICULTY_STARTING,
        )

    def validate(self, block, coefficient):
        with patch.object(validation, "verify_and_get_quality_string", return_value=self.quality):
            return validation.validate_finished_header_block(
                CONSTANTS, self.cache, get_block_header(block, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, coefficient,
            )[1]

    def test_parent_advance_reproduces_error_and_resigning_repairs_it(self):
        self.assertIsNone(self.validate(self.parent_block, Decimal(20)))
        old = self.finish(self.original)
        self.assertEqual(self.validate(old, Decimal(10)).code, Err.INVALID_TOTAL_ITERS)
        refreshed = self.refresh()
        self.assertIsNotNone(refreshed)
        unsigned = self.finish(refreshed)
        self.assertEqual(self.validate(unsigned, Decimal(10)).code, Err.INVALID_PLOT_SIGNATURE)
        signed = refreshed.replace(foliage=refreshed.foliage.replace(
            foliage_block_data_signature=AugSchemeMPL.sign(SK, refreshed.foliage.foliage_block_data.get_hash()),
        ))
        error = self.validate(self.finish(signed), Decimal(10))
        self.assertIsNone(error, error)
        self.assertEqual(signed.foliage.foliage_block_data.pool_target,
                         self.original.foliage.foliage_block_data.pool_target)
        self.assertEqual(signed.foliage.foliage_block_data.farmer_reward_puzzle_hash,
                         self.original.foliage.foliage_block_data.farmer_reward_puzzle_hash)

    def test_same_iterations_need_no_new_signature(self):
        self.assertIsNone(self.refresh(Decimal(20)))

    def test_both_coefficient_directions_are_refreshed(self):
        for original, coefficient in [(Decimal(20), Decimal(10)), (Decimal(10), Decimal(20))]:
            with self.subTest(original=original, coefficient=coefficient):
                self.original = self.candidate(original)
                refreshed = self.refresh(coefficient)
                signed = refreshed.replace(foliage=refreshed.foliage.replace(
                    foliage_block_data_signature=AugSchemeMPL.sign(SK, refreshed.foliage.foliage_block_data.get_hash()),
                ))
                self.assertIsNone(self.validate(self.finish(signed), coefficient))

    def test_original_parent_needs_no_refresh(self):
        self.assertIsNone(self.refresh(parent=self.old_parent))

    def test_passed_candidate_is_not_rewritten(self):
        self.assertIsNone(self.refresh(parent=self.parent.replace(total_iters=self.original.total_iters)))

    def test_unknown_branch_is_not_rewritten(self):
        from chia.util.hash import std_hash
        self.assertIsNone(self.refresh(parent=self.parent.replace(prev_hash=std_hash(b"other-branch"))))

    def test_foliage_signature_must_change(self):
        refreshed = self.refresh()
        self.assertEqual(refreshed.foliage.foliage_block_data_signature, G2Element())
        self.assertFalse(AugSchemeMPL.verify(
            PK, refreshed.foliage.foliage_block_data.get_hash(),
            self.original.foliage.foliage_block_data_signature,
        ))

    def test_transaction_body_is_preserved_if_previous_transaction_has_not_changed(self):
        parent = self.parent.replace(timestamp=None, prev_transaction_block_height=self.old_parent.height)
        self.records[parent.header_hash] = parent
        refreshed = self.refresh(parent=parent)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.foliage_transaction_block, self.original.foliage_transaction_block)
        self.assertEqual(refreshed.transactions_info, self.original.transactions_info)
        self.assertEqual(refreshed.transactions_generator, self.original.transactions_generator)
        self.assertEqual(refreshed.transactions_generator_ref_list, self.original.transactions_generator_ref_list)
        self.assertEqual(refreshed.foliage.foliage_transaction_block_signature,
                         self.original.foliage.foliage_transaction_block_signature)

    def test_transaction_body_is_removed_when_parent_transaction_is_after_sp(self):
        refreshed = self.refresh()
        self.assertIsNone(refreshed.foliage_transaction_block)
        self.assertIsNone(refreshed.transactions_info)
        self.assertIsNone(refreshed.transactions_generator)
        self.assertEqual(refreshed.transactions_generator_ref_list, [])

    def test_missing_transaction_body_is_not_manufactured(self):
        parent = self.parent.replace(timestamp=None, prev_transaction_block_height=self.old_parent.height)
        self.records[parent.header_hash] = parent
        self.original = self.original.replace(foliage_transaction_block=None)
        self.assertIsNone(self.refresh(parent=parent))

    def test_parent_before_signage_point_does_not_reuse_old_sp_signatures(self):
        self.original = self.original.replace(
            reward_chain_block=self.original.reward_chain_block.replace(signage_point_index=uint8(30))
        )
        self.assertIsNone(self.refresh())

    def test_overflow_waits_for_end_of_slot_then_uses_next_slot(self):
        index = uint8(63)
        self.original = self.original.replace(
            reward_chain_block=self.original.reward_chain_block.replace(
                signage_point_index=index, total_iters=uint128(CONSTANTS.SUB_SLOT_ITERS_STARTING + 150)
            )
        )
        parent = self.parent.replace(total_iters=uint128(4050))
        self.assertIsNone(self.refresh(parent=parent))
        info = VDFInfo(CONSTANTS.GENESIS_CHALLENGE, uint64(4096), ClassgroupElement.get_default_element())
        cc = ChallengeChainSubSlot(info, None, None, None, None)
        proof = VDFProof(uint8(0), b"", False)
        slot = EndOfSubSlotBundle(cc, None, RewardChainSubSlot(info, cc.get_hash(), None, uint8(16)),
                                  SubSlotProofs(proof, None, proof))
        self.store.finished_sub_slots = [(None, [], uint128(0)), (slot, [], uint128(4096))]
        with patch.object(self.store, "get_finished_sub_slots", return_value=[slot]) as slots:
            refreshed = self.refresh(parent=parent)
        self.assertIsNotNone(refreshed)
        slots.assert_called_once_with(self.cache, parent, cc.get_hash())
        required = calculate_iterations_quality(
            CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, QUALITY, 32, CONSTANTS.DIFFICULTY_STARTING,
            Decimal(10), self.signage.cc_vdf.output.get_hash(),
        )
        self.assertEqual(
            refreshed.total_iters,
            CONSTANTS.SUB_SLOT_ITERS_STARTING
            + calculate_ip_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, index, required),
        )

class CandidateRefreshFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = CandidateRefreshTest()
        self.fixture.setUp()
        self.parent = self.fixture.parent
        self.peer_id = std_hash(b"refresh-farmer")
        self.peer = SimpleNamespace(
            closed=False, connection_type=NodeType.FARMER, peer_node_id=self.peer_id, send_message=AsyncMock(),
        )
        self.store = self.fixture.store
        self.store.add_candidate_block(QUALITY, uint32(1), self.fixture.original)
        self.store.candidate_signing_peers[QUALITY] = self.peer_id
        self.node = FullNode(
            Path("/tmp/refresh-fixture"), {}, CONSTANTS, [], self.store, logging.getLogger("refresh-test"),
            Path("/tmp/refresh-fixture.db"), asyncio.Queue(),
        )
        self.node._timelord_lock = asyncio.Lock()
        self.node._server = SimpleNamespace(all_connections={self.peer_id: self.peer})
        self.chain = self.fixture.cache
        self.chain.get_peak = lambda: self.parent
        self.chain.get_farmer_difficulty_coeff = AsyncMock(return_value=Decimal(10))

        @asynccontextmanager
        async def acquire(**kwargs):
            yield

        self.chain.priority_mutex = SimpleNamespace(acquire=acquire)
        self.node._blockchain = self.chain
        self.node.add_unfinished_block = AsyncMock()
        self.node.has_valid_pool_sig = Mock(return_value=True)
        self.api = FullNodeAPI(self.node)
        self.addCleanup(self.api.executor.shutdown)
        self.proof_patch = patch("chia.full_node.candidate_refresh.verify_and_get_quality_string", return_value=QUALITY)
        self.proof_patch.start()
        self.addCleanup(self.proof_patch.stop)

    async def test_new_signature_is_requested_before_candidate_is_published(self):
        self.assertTrue(await self.node.refresh_candidate_block(QUALITY))
        self.peer.send_message.assert_awaited_once()
        message = self.peer.send_message.call_args.args[0]
        self.assertEqual(message.type, ProtocolMessageTypes.request_signed_values.value)
        request = farmer_protocol.RequestSignedValues.from_bytes(message.data)
        candidate = self.store.get_candidate_block(QUALITY)[1]
        self.assertEqual(request.rc_block_unfinished, candidate.reward_chain_block)
        self.assertEqual(request.foliage_block_data_hash, candidate.foliage.foliage_block_data.get_hash())
        self.node.add_unfinished_block.assert_not_awaited()
        stale = farmer_protocol.SignedValues(
            QUALITY, self.fixture.original.foliage.foliage_block_data_signature, G2Element(),
        )
        await self.api.signed_values(bytes(stale), self.peer)
        self.node.add_unfinished_block.assert_not_awaited()
        signed = farmer_protocol.SignedValues(
            QUALITY, AugSchemeMPL.sign(SK, request.foliage_block_data_hash), G2Element(),
        )
        await self.api.signed_values(bytes(signed), self.peer)
        self.node.add_unfinished_block.assert_awaited_once()
        candidate = self.node.add_unfinished_block.call_args.args[0]
        self.assertIsNone(self.fixture.validate(self.fixture.finish(candidate), Decimal(10)))

    async def test_duplicate_refresh_does_not_request_signature_again(self):
        await self.node.refresh_candidate_block(QUALITY)
        self.assertFalse(await self.node.refresh_candidate_block(QUALITY))
        self.peer.send_message.assert_awaited_once()

    async def test_remote_candidates_cannot_be_resigned_locally(self):
        self.store.candidate_signing_peers.clear()
        self.assertFalse(await self.node.refresh_candidate_block(QUALITY))
        self.peer.send_message.assert_not_awaited()
        self.assertEqual(self.store.get_candidate_block(QUALITY)[1], self.fixture.original)

    async def test_disconnected_farmer_keeps_original_candidate(self):
        self.peer.closed = True
        self.assertFalse(await self.node.refresh_candidate_block(QUALITY))
        self.assertEqual(self.store.get_candidate_block(QUALITY)[1], self.fixture.original)

    async def test_sync_mode_does_not_sign(self):
        self.node.sync_store.set_sync_mode(True)
        self.assertFalse(await self.node.refresh_candidate_block(QUALITY))
        self.peer.send_message.assert_not_awaited()

    def test_eviction_clears_signing_peer(self):
        self.store.clear_candidate_blocks_below(uint32(2))
        self.assertNotIn(QUALITY, self.store.candidate_signing_peers)

    async def test_refresh_does_not_wait_for_signatures_under_locks(self):
        async def check_locks(message):
            self.assertFalse(self.node.timelord_lock.locked())

        self.peer.send_message.side_effect = check_locks
        await self.node.refresh_candidate_block(QUALITY)

    async def test_coefficient_changes_twice_before_signature_reply(self):
        await self.node.refresh_candidate_block(QUALITY)
        first = farmer_protocol.RequestSignedValues.from_bytes(self.peer.send_message.call_args.args[0].data)
        self.parent = self.parent.replace(header_hash=std_hash(b"second-parent"), prev_hash=self.parent.header_hash,
                                          height=uint32(self.parent.height + 1),
                                          weight=uint128(self.parent.weight + CONSTANTS.DIFFICULTY_STARTING))
        self.chain.get_farmer_difficulty_coeff.return_value = Decimal(20)
        self.assertTrue(await self.node.refresh_candidate_block(QUALITY))
        latest = farmer_protocol.RequestSignedValues.from_bytes(self.peer.send_message.call_args.args[0].data)
        self.assertNotEqual(first.foliage_block_data_hash, latest.foliage_block_data_hash)
        await self.api.signed_values(bytes(farmer_protocol.SignedValues(
            QUALITY, AugSchemeMPL.sign(SK, first.foliage_block_data_hash), G2Element(),
        )), self.peer)
        self.node.add_unfinished_block.assert_not_awaited()
        await self.api.signed_values(bytes(farmer_protocol.SignedValues(
            QUALITY, AugSchemeMPL.sign(SK, latest.foliage_block_data_hash), G2Element(),
        )), self.peer)
        self.node.add_unfinished_block.assert_awaited_once()

    async def test_end_of_slot_during_refresh_requests_another_pass(self):
        calls = 0

        async def refresh(quality):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.node._schedule_candidate_refresh()
            return False

        self.node.refresh_candidate_block = refresh
        self.node._schedule_candidate_refresh()
        await self.node._candidate_refresh_task
        self.assertEqual(calls, 2)

    async def test_candidate_contexts_are_sent_before_peak_in_one_batch(self):
        candidate = self.fixture.original
        required = uint64(1)
        self.store.add_unfinished_block(
            uint32(1), candidate, PreValidationResult(None, required, None, False, uint32(0))
        )
        self.chain.get_next_difficulty = Mock(return_value=CONSTANTS.DIFFICULTY_STARTING)
        self.chain.get_recent_reward_challenges = Mock(return_value=[])
        self.node.server.send_to_all = AsyncMock()
        with patch("chia.full_node.full_node.next_sub_epoch_summary", return_value=None) as summary:
            await self.node.send_peak_to_timelords(self.fixture.parent_block)
        self.node.server.send_to_all.assert_awaited_once()
        messages, target = self.node.server.send_to_all.call_args.args
        self.assertEqual(target, NodeType.TIMELORD)
        self.assertEqual([message.type for message in messages], [
            ProtocolMessageTypes.new_unfinished_block_timelord.value,
            ProtocolMessageTypes.new_peak_timelord.value,
        ])
        context = timelord_protocol.NewUnfinishedBlockTimelord.from_bytes(messages[0].data)
        self.assertEqual(context.difficulty_coeff, "10")
        self.assertEqual(context.reward_chain_block, candidate.reward_chain_block)
        self.assertEqual(summary.call_args_list[0].args[2:4], (required, candidate))

    async def test_peak_change_while_reading_stake_cannot_forward_old_context(self):
        first = self.parent
        latest = self.parent.replace(
            header_hash=std_hash(b"peak-during-coefficient-read"), height=uint32(self.parent.height + 1)
        )

        async def coefficient(farmer, height):
            if height == first.height:
                self.parent = latest
                return Decimal(10)
            return Decimal(5)

        self.chain.get_farmer_difficulty_coeff.side_effect = coefficient
        actual = await self.node._candidate_scheduling_coefficient(self.fixture.original, Decimal(20))
        self.assertEqual(actual, Decimal(5))
        self.assertEqual(self.chain.get_farmer_difficulty_coeff.await_count, 2)


class TimelordContextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = iteration_fixture.TimelordAccountingTest()
        self.fixture.setUp()
        self.timelord = self.fixture.timelord
        self.timelord.lock = asyncio.Lock()
        self.timelord.bluebox_mode = False
        self.timelord.overflow_blocks = []
        self.timelord.unfinished_blocks = []
        self.timelord.iters_to_submit = {chain: [] for chain in list(Chain)[:3]}
        self.timelord.iteration_to_proof_type = {}
        self.timelord.total_unfinished = 0
        self.timelord.candidate_coefficients = LRUCache(1000)
        self.api = TimelordAPI(self.timelord)
        self.peer = SimpleNamespace(
            closed=False, is_outbound=True, connection_type=NodeType.FULL_NODE,
            peer_info=SimpleNamespace(host="127.0.0.1"),
        )
        self.proof_patch = patch("chia.timelord.iters_from_block.verify_and_get_quality_string", return_value=QUALITY)
        self.proof_patch.start()
        self.addCleanup(self.proof_patch.stop)

    async def test_updated_context_retires_old_signed_candidate_without_advancement(self):
        candidate, ip = self.fixture.candidate()
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [candidate])
        changed = dataclasses.replace(candidate, difficulty_coeff="10")
        await self.api.new_unfinished_block_timelord(bytes(changed), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [])
        # The already-submitted IP still completes, then must retire without blocking SPs.
        iteration = uint64(ip - self.fixture.state.last_ip)
        self.timelord.proofs_finished = [
            (chain, VDFInfo(self.fixture.state.get_challenge(chain), iteration,
                            ClassgroupElement.get_default_element()), VDFProof(uint8(0), b"", False),
             self.timelord.num_resets)
            for chain in list(Chain)[:3]
        ]
        await self.timelord._check_for_new_ip(iteration)
        self.assertIn(iteration, self.timelord.iters_finished)
        self.timelord._handle_new_peak.assert_not_awaited()

    async def test_remote_replay_cannot_restore_context_rejected_by_local_node(self):
        candidate, _ = self.fixture.candidate()
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        changed = dataclasses.replace(candidate, difficulty_coeff="10")
        await self.api.new_unfinished_block_timelord(bytes(changed), self.peer)
        self.peer.peer_info.host = "203.0.113.1"
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [])

    async def test_duplicate_messages_do_not_duplicate_work(self):
        candidate, _ = self.fixture.candidate()
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [candidate])
        self.assertEqual(self.timelord.total_unfinished, 1)

    async def test_rounding_equivalent_context_preserves_submitted_proofs(self):
        candidate, _ = self.fixture.candidate()
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        self.timelord.proofs_finished = [object()]
        changed = dataclasses.replace(candidate, difficulty_coeff="20.00000001")
        await self.api.new_unfinished_block_timelord(bytes(changed), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [changed])
        self.assertEqual(len(self.timelord.proofs_finished), 1)

    async def test_rounding_equivalent_overflow_survives_slot_origin_change(self):
        candidate, _ = self.fixture.candidate(overflow=True)
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        self.fixture.state.total_iters = uint128(self.fixture.state.sub_slot_iters * 2)
        changed = dataclasses.replace(candidate, difficulty_coeff="20.00000001")
        await self.api.new_unfinished_block_timelord(bytes(changed), self.peer)
        self.assertEqual(self.timelord.overflow_blocks, [changed])

    async def test_coefficient_exceeding_proof_interval_retires_old_work(self):
        candidate, _ = self.fixture.candidate()
        await self.api.new_unfinished_block_timelord(bytes(candidate), self.peer)
        with patch("chia.timelord.timelord_api.iters_from_block", side_effect=ValueError("Required iters too large")):
            changed = dataclasses.replace(candidate, difficulty_coeff="10")
            await self.api.new_unfinished_block_timelord(bytes(changed), self.peer)
        self.assertEqual(self.timelord.unfinished_blocks, [])

    async def test_unconfirmed_parent_blocks_second_infusion_until_local_confirmation(self):
        candidate, ip = self.fixture.candidate()
        self.timelord.pending_local_infusion = (self.fixture.state.peak, self.peer, 1000.0)
        await self.fixture.submit_proofs(candidate, ip)
        self.timelord._server.send_to_all.assert_not_awaited()
        self.assertNotIn(ip - self.fixture.state.last_ip, self.timelord.iters_finished)
        self.timelord.pending_peak_recovery = None
        self.timelord.state_changed = Mock()
        confirmation = dataclasses.replace(
            self.fixture.state.peak,
            previous_reward_challenges=[(bytes32(challenge), total) for challenge, total
                                        in self.fixture.state.peak.previous_reward_challenges],
        )
        await self.api.new_peak_timelord(bytes(confirmation), self.peer)
        self.assertIsNone(self.timelord.pending_local_infusion)
        await self.timelord._check_for_new_ip(uint64(ip - self.fixture.state.last_ip))
        self.timelord._server.send_to_all.assert_awaited_once()
        self.assertIsNotNone(self.timelord.pending_local_infusion)

    async def test_remote_confirmation_cannot_clear_local_parent_wait(self):
        self.timelord.pending_local_infusion = (self.fixture.state.peak, self.peer, 1000.0)
        self.timelord.pending_peak_recovery = None
        self.timelord.state_changed = Mock()
        self.peer.peer_info.host = "203.0.113.1"
        await self.api.new_peak_timelord(self.fixture.state.peak, self.peer)
        self.assertIsNotNone(self.timelord.pending_local_infusion)

    async def test_missing_confirmation_recovers_to_confirmed_parent_after_timeout(self):
        candidate, ip = self.fixture.candidate()
        confirmed = self.fixture.state.peak
        self.timelord.pending_local_infusion = (confirmed, self.peer, 1000.0)
        self.timelord.pending_peak_recovery = None
        self.timelord.overflow_blocks = [object()]
        await self.fixture.submit_proofs(candidate, ip)
        with patch("chia.timelord.timelord.time.monotonic", return_value=1014.999):
            self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.new_peak)
        self.assertIsNotNone(self.timelord.pending_local_infusion)
        self.assertTrue(self.timelord.unfinished_blocks)
        with patch("chia.timelord.timelord.time.monotonic", return_value=1015.0):
            with self.assertLogs("chia.timelord.timelord", level="WARNING") as logs:
                self.timelord._check_for_peak_recovery()
        self.assertEqual(self.timelord.new_peak, confirmed)
        self.assertIsNone(self.timelord.pending_local_infusion)
        self.assertEqual(self.timelord.unfinished_blocks, [])
        self.assertEqual(self.timelord.overflow_blocks, [])
        self.timelord._server.send_to_all.assert_not_awaited()
        self.assertIn("recovering to confirmed peak", "\n".join(logs.output))

    async def test_disconnected_local_parent_recovers_without_waiting_or_using_remote_context(self):
        confirmed = self.fixture.state.peak
        self.timelord.confirmed_local_peak = (confirmed, self.peer)
        self.timelord.pending_local_infusion = (confirmed, self.peer, 1000.0)
        self.timelord.pending_peak_recovery = None
        self.timelord.candidate_coefficients.put(std_hash(b"old-work"), "20")
        self.peer.closed = True
        with patch("chia.timelord.timelord.time.monotonic", return_value=1000.1):
            self.timelord._check_for_peak_recovery()
        self.assertEqual(self.timelord.new_peak, confirmed)
        self.assertIsNone(self.timelord.pending_local_infusion)
        self.assertIsNone(self.timelord.confirmed_local_peak)
        self.assertIsNone(self.timelord.candidate_coefficients.get(std_hash(b"old-work")))

    async def test_confirmed_parent_history_is_not_mutated_by_speculative_or_recovery_state(self):
        confirmation = dataclasses.replace(
            self.fixture.state.peak,
            previous_reward_challenges=list(self.fixture.state.peak.previous_reward_challenges),
        )
        self.timelord.pending_peak_recovery = None
        self.timelord.state_changed = Mock()
        await self.api.new_peak_timelord(confirmation, self.peer)
        saved, _ = self.timelord.confirmed_local_peak
        expected = list(saved.previous_reward_challenges)
        confirmation.previous_reward_challenges.append((std_hash(b"speculative"), uint128(999)))
        self.assertEqual(saved.previous_reward_challenges, expected)
        self.timelord.pending_local_infusion = (saved, self.peer, 1000.0)
        with patch("chia.timelord.timelord.time.monotonic", return_value=1015.0):
            self.timelord._check_for_peak_recovery()
        self.timelord.new_peak.previous_reward_challenges.append((std_hash(b"later-eos"), uint128(1001)))
        self.assertEqual(saved.previous_reward_challenges, expected)


class CandidateChainAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_refreshed_signed_candidate_passes_body_validation_and_enters_database(self):
        fixture = CandidateRefreshTest()
        fixture.setUp()
        candidate = fixture.refresh()
        candidate = candidate.replace(foliage=candidate.foliage.replace(
            foliage_block_data_signature=AugSchemeMPL.sign(SK, candidate.foliage.foliage_block_data.get_hash()),
        ))
        refreshed = fixture.finish(candidate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with DBWrapper2.managed(root / "chain.sqlite", db_version=2, reader_count=1) as database:
                chain = await Blockchain.create(
                    await CoinStore.create(database), await BlockStore.create(database),
                    CONSTANTS, root, 0, single_threaded=True,
                )
                try:
                    for block, coefficient in [
                        (fixture.genesis, Decimal(20)), (fixture.parent_block, Decimal(20)),
                        (refreshed, Decimal(10)),
                    ]:
                        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY):
                            required, error = validation.validate_finished_header_block(
                                CONSTANTS, chain, get_block_header(block, [], []), False,
                                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, coefficient,
                            )
                        self.assertIsNone(error)
                        result, error, _ = await chain.add_block(
                            block, PreValidationResult(None, required, None, False, uint32(0)), None,
                        )
                        self.assertEqual((result, error), (AddBlockResult.NEW_PEAK, None))
                    self.assertEqual(chain.get_peak().header_hash, refreshed.header_hash)
                    self.assertEqual(await chain.block_store.get_full_block(refreshed.header_hash), refreshed)
                finally:
                    chain.shut_down()


if __name__ == "__main__":
    unittest.main(verbosity=2)
