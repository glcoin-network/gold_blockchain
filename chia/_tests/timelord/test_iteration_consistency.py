"""Iteration accounting regressions using the installed Gold implementation."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from decimal import Decimal
import logging
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gold_rs import AugSchemeMPL, G1Element, G2Element

from chia.consensus import block_header_validation as validation
from chia.consensus import multiprocess_validation as batch_validation
from chia.consensus.block_creation import create_unfinished_block, unfinished_block_to_full_block
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.full_block_to_block_record import block_to_block_record
from chia.consensus.pot_iterations import calculate_ip_iters, calculate_iterations_quality, calculate_sp_iters
from chia.full_node import full_node_api as node_api
from chia.full_node.signage_point import SignagePoint
from chia.protocols import timelord_protocol
from chia.timelord import timelord as timelord_module
from chia.timelord.iters_from_block import iters_from_block
from chia.timelord.timelord import Timelord
from chia.timelord.timelord_state import LastState
from chia.timelord.types import Chain, IterationType
from chia.types.blockchain_format.classgroup import ClassgroupElement
from chia.types.blockchain_format.pool_target import PoolTarget
from chia.types.blockchain_format.proof_of_space import ProofOfSpace
from chia.types.blockchain_format.vdf import VDFInfo, VDFProof
from chia.util.block_cache import BlockCache
from chia.util.errors import Err
from chia.util.generator_tools import get_block_header
from chia.util.hash import std_hash
from chia.util.ints import uint8, uint16, uint32, uint64, uint128
from chia.util.vdf_prover import get_vdf_info_and_proof


CONSTANTS = DEFAULT_CONSTANTS.replace(
    DISCRIMINANT_SIZE_BITS=uint16(16),
    SUB_SLOT_ITERS_STARTING=uint64(4096),
    DIFFICULTY_CONSTANT_FACTOR=uint128(2**35),
)
QUALITY = std_hash(b"iteration-consistency-quality")
SK = AugSchemeMPL.key_gen(std_hash(b"iteration-consistency-key"))
PK = SK.get_g1()


def sign(message, _pk):
    return AugSchemeMPL.sign(SK, message)


def proof_of_space():
    # Space verification alone is stubbed; iteration arithmetic, BLS and VDFs are real.
    return ProofOfSpace(CONSTANTS.GENESIS_CHALLENGE, PK, None, G1Element(), uint8(32), b"", PK)


def make_genesis():
    cache = BlockCache({})
    required = calculate_iterations_quality(
        CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, QUALITY, 32,
        CONSTANTS.DIFFICULTY_STARTING, Decimal(20), CONSTANTS.GENESIS_CHALLENGE,
    )
    ip = calculate_ip_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, uint8(0), required)
    unfinished = create_unfinished_block(
        CONSTANTS, uint128(0), CONSTANTS.SUB_SLOT_ITERS_STARTING, uint8(0), uint64(0), ip,
        proof_of_space(), CONSTANTS.GENESIS_CHALLENGE,
        CONSTANTS.GENESIS_PRE_FARM_FARMER_PUZZLE_HASH,
        PoolTarget(CONSTANTS.GENESIS_PRE_FARM_POOL_PUZZLE_HASH, uint32(0)),
        sign, lambda target, _pk: AugSchemeMPL.sign(SK, bytes(target)),
        SignagePoint(None, None, None, None), uint64(1), cache,
    )
    info, proof = get_vdf_info_and_proof(
        CONSTANTS, ClassgroupElement.get_default_element(), CONSTANTS.GENESIS_CHALLENGE, ip,
    )
    full = unfinished_block_to_full_block(
        unfinished, info, proof, info, proof, None, None, [], None, cache,
        uint128(0), CONSTANTS.DIFFICULTY_STARTING,
    )
    return full, block_to_block_record(CONSTANTS, cache, required, full, None)


class HeaderAccountingTest(unittest.TestCase):
    def test_real_bls_vdf_header_and_total_iters_rejection(self):
        full, _ = make_genesis()
        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY):
            required, error = validation.validate_finished_header_block(
                CONSTANTS, BlockCache({}), get_block_header(full, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, Decimal(20),
            )
            self.assertIsNone(error)
            self.assertIsNotNone(required)
            modified = full.replace(
                reward_chain_block=full.reward_chain_block.replace(total_iters=uint128(full.total_iters + 1))
            )
            _, error = validation.validate_finished_header_block(
                CONSTANTS, BlockCache({}), get_block_header(modified, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, Decimal(20),
            )
            self.assertEqual(error.code, Err.INVALID_TOTAL_ITERS)

    def test_changed_effective_coefficient_rejects_committed_iterations(self):
        full, _ = make_genesis()
        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY):
            _, error = validation.validate_finished_header_block(
                CONSTANTS, BlockCache({}), get_block_header(full, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, Decimal(1),
            )
        self.assertEqual(error.code, Err.INVALID_TOTAL_ITERS)

    def test_small_coefficient_change_with_same_integer_keeps_iterations(self):
        args = (CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, QUALITY, 32, uint64(312))
        a = calculate_iterations_quality(*args, Decimal("0.5288095111036109379665270111"), QUALITY)
        b = calculate_iterations_quality(*args, Decimal("0.5288007369458956707333348625"), QUALITY)
        self.assertEqual(a, b)

    def test_bad_plot_signature_still_rejected(self):
        full, _ = make_genesis()
        full = full.replace(foliage=full.foliage.replace(foliage_block_data_signature=G2Element()))
        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY):
            _, error = validation.validate_finished_header_block(
                CONSTANTS, BlockCache({}), get_block_header(full, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, Decimal(20),
            )
        self.assertEqual(error.code, Err.INVALID_PLOT_SIGNATURE)

    def test_bad_native_vdf_still_rejected(self):
        full, _ = make_genesis()
        full = full.replace(challenge_chain_ip_proof=VDFProof(uint8(0), b"", False))
        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY):
            _, error = validation.validate_finished_header_block(
                CONSTANTS, BlockCache({}), get_block_header(full, [], []), False,
                CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING, Decimal(20),
            )
        self.assertEqual(error.code, Err.INVALID_CC_IP_VDF)

    def test_batch_rejection_keeps_expected_actual_and_context_in_log(self):
        full, _ = make_genesis()
        actual = full.total_iters + 1
        modified = full.replace(reward_chain_block=full.reward_chain_block.replace(total_iters=uint128(actual)))
        with patch.object(validation, "verify_and_get_quality_string", return_value=QUALITY), self.assertLogs(
            batch_validation.log, level="WARNING",
        ) as logs:
            serialized = batch_validation.batch_pre_validate_blocks(
                BlockCache({}), CONSTANTS, {}, [bytes(modified)], [None], {}, False,
                [CONSTANTS.DIFFICULTY_STARTING], [CONSTANTS.SUB_SLOT_ITERS_STARTING], [Decimal(20)], False,
            )
        result = batch_validation.PreValidationResult.from_bytes(serialized[0])
        self.assertEqual(result.error, Err.INVALID_TOTAL_ITERS.value)
        message = "\n".join(logs.output)
        self.assertIn(f"expected {full.total_iters} got {actual}", message)
        self.assertIn(modified.prev_header_hash.hex(), message)
        self.assertIn("coefficient 20", message)


class NativeProofActivityTest(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, invalid=False, stale_label=False, wrong_challenge=False,
                       unrequested=False, duplicate=False, completed=False):
        chain = Chain.CHALLENGE_CHAIN
        challenge = CONSTANTS.GENESIS_CHALLENGE
        initial = ClassgroupElement.get_default_element()
        iterations = uint64(128)
        info, proof = get_vdf_info_and_proof(CONSTANTS, initial, challenge, iterations)
        if invalid:
            proof = proof.replace(witness=b"")
        label = 2 if stale_label else 3
        payload = (
            int(iterations).to_bytes(8, "big", signed=True)
            + len(info.output.data).to_bytes(8, "big", signed=True)
            + info.output.data + bytes([proof.witness_type]) + proof.witness
        ).hex().encode()
        reader = asyncio.StreamReader()
        reader.feed_data(b"OK" + len(payload).to_bytes(4, "big") + payload + b"STOP")
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        node = Timelord.__new__(Timelord)
        node.constants = CONSTANTS
        node.config = {"fast_algorithm": False}
        node.lock = asyncio.Lock()
        node.bluebox_mode = False
        node.num_resets = 3
        node.last_state = SimpleNamespace(
            get_challenge=lambda _: std_hash(b"other-task") if wrong_challenge else challenge
        )
        node.last_active_time = 0
        node.max_allowed_inactivity_time = 60
        node.vdf_failure_time = 0
        node.vdf_failures = []
        node.iters_submitted = {chain: [] if unrequested else [iterations]}
        node.iters_finished = {iterations} if completed else set()
        node.proofs_finished = [(chain, info, proof, label)] if duplicate else []
        node.allows_iters = []
        node.chain_start_time = {chain: time.time() - 1}
        node.state_changed_callback = None
        node._reset_chains = AsyncMock()
        await node._do_process_communication(
            chain, challenge, initial, "127.0.0.1", reader, writer, proof_label=label,
        )
        await node._handle_failures()
        return node

    async def test_valid_requested_native_proof_keeps_watchdog_alive_before_pair_completes(self):
        node = await self.exercise()
        self.assertGreater(node.last_active_time, 0)
        self.assertEqual(node.iters_finished, set())
        node._reset_chains.assert_not_awaited()
        self.assertEqual(node.max_allowed_inactivity_time, 60)

    async def test_invalid_native_proof_does_not_keep_watchdog_alive(self):
        with self.assertLogs(timelord_module.log, level="ERROR") as records:
            node = await self.exercise(invalid=True)
        self.assertIn("Invalid proof of time!", "\n".join(records.output))
        node._reset_chains.assert_awaited_once()
        self.assertEqual(node.last_active_time, 0)

    async def test_stale_proof_label_does_not_keep_watchdog_alive(self):
        node = await self.exercise(stale_label=True)
        node._reset_chains.assert_awaited_once()

    async def test_old_challenge_proof_does_not_keep_watchdog_alive(self):
        node = await self.exercise(wrong_challenge=True)
        node._reset_chains.assert_awaited_once()

    async def test_unrequested_proof_does_not_keep_watchdog_alive(self):
        node = await self.exercise(unrequested=True)
        node._reset_chains.assert_awaited_once()

    async def test_duplicate_native_proof_does_not_keep_watchdog_alive(self):
        node = await self.exercise(duplicate=True)
        node._reset_chains.assert_awaited_once()

    async def test_already_completed_proof_does_not_keep_watchdog_alive(self):
        node = await self.exercise(completed=True)
        node._reset_chains.assert_awaited_once()


class TimelordAccountingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        full, _ = make_genesis()
        # The higher fixture height exercises speculative advancement without pre-farm restrictions.
        full = full.replace(reward_chain_block=full.reward_chain_block.replace(height=uint32(100)))
        self.parent = full
        self.state = LastState(CONSTANTS)
        with patch("chia.timelord.iters_from_block.verify_and_get_quality_string", return_value=QUALITY):
            self.state.set_state(timelord_protocol.NewPeakTimelord(
                full.reward_chain_block, CONSTANTS.DIFFICULTY_STARTING, "20",
                uint8(CONSTANTS.MIN_BLOCKS_PER_CHALLENGE_BLOCK - 1),
                CONSTANTS.SUB_SLOT_ITERS_STARTING, None, [],
                uint128(full.total_iters), False,
            ))
        self.timelord = Timelord.__new__(Timelord)
        self.timelord.constants = CONSTANTS
        self.timelord.last_state = self.state
        self.timelord.num_resets = 3
        self.timelord._server = SimpleNamespace(send_to_all=AsyncMock())
        self.timelord.new_peak = None
        self.timelord.confirmed_local_peak = None
        self.timelord.pending_local_infusion = None
        self.timelord._handle_new_peak = AsyncMock()
        self.timelord.iters_finished = set()
        self.timelord.last_active_time = 0
        self.timelord.proofs_finished = []

    def candidate(self, *, offset=0, overflow=False):
        index = uint8(63 if overflow else 16)
        sp_iters = calculate_sp_iters(CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, index)
        sp_info = VDFInfo(
            CONSTANTS.GENESIS_CHALLENGE, sp_iters, ClassgroupElement.get_default_element(),
        )
        reward = self.parent.reward_chain_block.get_unfinished().replace(
            signage_point_index=index, challenge_chain_sp_vdf=sp_info,
        )
        with patch("chia.timelord.iters_from_block.verify_and_get_quality_string", return_value=QUALITY):
            _, ip = iters_from_block(
                CONSTANTS, reward, CONSTANTS.SUB_SLOT_ITERS_STARTING,
                CONSTANTS.DIFFICULTY_STARTING, uint32(101), Decimal(20),
            )
        slot_start = self.state.total_iters - self.state.last_ip
        if overflow:
            slot_start += self.state.sub_slot_iters
        reward = reward.replace(total_iters=uint128(slot_start + ip + offset))
        self.state.reward_challenge_cache = [
            (CONSTANTS.GENESIS_CHALLENGE, uint128(0)),
            (self.parent.reward_chain_block.get_hash(), self.state.total_iters),
        ]
        return timelord_protocol.NewUnfinishedBlockTimelord(
            reward, CONSTANTS.DIFFICULTY_STARTING, CONSTANTS.SUB_SLOT_ITERS_STARTING,
            self.parent.foliage, None, self.parent.reward_chain_block.get_hash(), "20",
        ), ip

    def check_candidate(self, candidate):
        with patch("chia.timelord.iters_from_block.verify_and_get_quality_string", return_value=QUALITY):
            return self.timelord._can_infuse_unfinished_block(candidate)

    def test_matching_non_overflow_candidate_scheduled(self):
        candidate, ip = self.candidate()
        self.assertEqual(self.check_candidate(candidate), ip - self.state.last_ip)

    def test_mismatched_committed_total_not_scheduled(self):
        candidate, _ = self.candidate(offset=1)
        self.assertIsNone(self.check_candidate(candidate))

    def test_wrong_slot_candidate_not_scheduled(self):
        candidate, _ = self.candidate(offset=CONSTANTS.SUB_SLOT_ITERS_STARTING)
        self.assertIsNone(self.check_candidate(candidate))

    def test_next_slot_overflow_waits_then_schedules(self):
        candidate, ip = self.candidate(overflow=True)
        with patch.object(timelord_module.log, "warning") as warning:
            self.assertIsNone(self.check_candidate(candidate))
        warning.assert_not_called()
        self.state.total_iters = uint128(self.state.sub_slot_iters)
        self.state.last_ip = uint64(0)
        self.assertEqual(self.check_candidate(candidate), ip)

    def test_unknown_reward_challenge_still_rejected(self):
        candidate, _ = self.candidate()
        candidate = dataclasses.replace(candidate, rc_prev=std_hash(b"unknown-reward"))
        self.assertIsNone(self.check_candidate(candidate))

    def test_invalid_coefficient_still_rejected(self):
        candidate, _ = self.candidate()
        candidate = dataclasses.replace(candidate, difficulty_coeff="not-a-number")
        self.assertIsNone(self.check_candidate(candidate))

    async def submit_proofs(self, candidate, ip, *, additional=(), stale_proofs=False):
        iteration = uint64(ip - self.state.last_ip)
        self.timelord.unfinished_blocks = [candidate, *additional]
        self.timelord.iteration_to_proof_type = {iteration: IterationType.INFUSION_POINT}
        proof = VDFProof(uint8(0), b"", False)
        self.timelord.proofs_finished = [
            (chain, VDFInfo(self.state.get_challenge(chain), iteration,
                            ClassgroupElement.get_default_element()), proof,
             self.timelord.num_resets - int(stale_proofs))
            for chain in [Chain.CHALLENGE_CHAIN, Chain.REWARD_CHAIN, Chain.INFUSED_CHALLENGE_CHAIN]
        ]
        with patch("chia.timelord.iters_from_block.verify_and_get_quality_string", return_value=QUALITY):
            await self.timelord._check_for_new_ip(iteration)

    async def test_mismatch_at_proof_completion_does_not_publish_or_advance(self):
        candidate, ip = self.candidate(offset=1)
        await self.submit_proofs(candidate, ip)
        self.timelord._server.send_to_all.assert_not_awaited()
        self.timelord._handle_new_peak.assert_not_awaited()
        self.assertIsNone(self.timelord.new_peak)
        self.assertIn(ip - self.state.last_ip, self.timelord.iters_finished)
        self.assertEqual(self.timelord.proofs_finished, [])

    async def test_matching_proof_still_publishes_and_advances(self):
        candidate, ip = self.candidate()
        await self.submit_proofs(candidate, ip)
        self.timelord._server.send_to_all.assert_awaited_once()
        self.timelord._handle_new_peak.assert_awaited_once()
        self.assertEqual(self.timelord.new_peak.reward_chain_block.total_iters,
                         candidate.reward_chain_block.total_iters)

    async def test_invalid_candidate_does_not_hide_valid_candidate_at_same_iteration(self):
        valid, ip = self.candidate()
        invalid, _ = self.candidate(offset=1)
        await self.submit_proofs(invalid, ip, additional=[valid])
        self.timelord._server.send_to_all.assert_awaited_once()
        self.assertEqual(self.timelord.new_peak.reward_chain_block.get_unfinished(), valid.reward_chain_block)

    async def test_all_unverifiable_candidates_do_not_block_later_iterations(self):
        candidate, ip = self.candidate()
        with patch.object(timelord_module, "iters_from_block", side_effect=ValueError("fixture bad proof")):
            await self.submit_proofs(candidate, ip)
        self.timelord._server.send_to_all.assert_not_awaited()
        self.assertIn(ip - self.state.last_ip, self.timelord.iters_finished)

    async def test_proofs_from_old_reset_do_not_publish_or_advance(self):
        candidate, ip = self.candidate()
        await self.submit_proofs(candidate, ip, stale_proofs=True)
        self.timelord._server.send_to_all.assert_not_awaited()
        self.timelord._handle_new_peak.assert_not_awaited()


class CandidateHeightTest(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_coefficient_uses_selected_parent_height(self):
        genesis, _ = make_genesis()
        parent = SimpleNamespace(
            height=uint32(200), reward_infusion_new_challenge=std_hash(b"parent-rc"),
            header_hash=std_hash(b"parent"), prev_hash=std_hash(b"older-parent"),
            finished_reward_slot_hashes=None, is_transaction_block=True,
            timestamp=uint64(1), weight=uint128(1400),
            sub_slot_iters=CONSTANTS.SUB_SLOT_ITERS_STARTING,
        )
        peak = SimpleNamespace(
            height=uint32(201), reward_infusion_new_challenge=std_hash(b"peak-rc"),
            header_hash=std_hash(b"peak"), prev_hash=parent.header_hash,
            finished_reward_slot_hashes=None, is_transaction_block=True,
            timestamp=uint64(2), weight=uint128(1407),
            sub_slot_iters=CONSTANTS.SUB_SLOT_ITERS_STARTING,
        )

        @asynccontextmanager
        async def acquire(**kwargs):
            yield

        async def coefficient(pk, height=None):
            return Decimal("0.5") if height == parent.height else Decimal(20)

        rc_sp = VDFInfo(parent.reward_infusion_new_challenge, uint64(1),
                        ClassgroupElement.get_default_element())
        cc_sp = rc_sp.replace(challenge=CONSTANTS.GENESIS_CHALLENGE)
        store = SimpleNamespace(
            get_signage_point=Mock(return_value=SignagePoint(cc_sp, None, rc_sp, None)),
            finished_sub_slots=[], get_finished_sub_slots=Mock(return_value=[]),
            add_candidate_block=Mock(), previous_generator=None,
            candidate_signing_peers={},
        )
        chain = SimpleNamespace(
            get_peak=lambda: peak,
            priority_mutex=SimpleNamespace(acquire=acquire),
            try_block_record=lambda hh: parent if hh == parent.header_hash else None,
            block_record=lambda hh: parent,
            get_farmer_difficulty_coeff=AsyncMock(side_effect=coefficient),
        )
        full_node = SimpleNamespace(
            sync_store=SimpleNamespace(get_sync_mode=lambda: False),
            timelord_lock=asyncio.Lock(), full_node_store=store, blockchain=chain,
            constants=CONSTANTS, mempool_manager=SimpleNamespace(
                create_bundle_from_mempool=AsyncMock(return_value=None)),
            coin_store=SimpleNamespace(get_unspent_lineage_info_for_puzzle_hash=None),
            log=logging.getLogger("candidate-test"),
        )
        api = node_api.FullNodeAPI(full_node)
        self.addCleanup(api.executor.shutdown)
        request = SimpleNamespace(
            challenge_chain_sp=cc_sp.output.get_hash(), reward_chain_sp=rc_sp.output.get_hash(),
            signage_point_index=uint8(1), challenge_hash=CONSTANTS.GENESIS_CHALLENGE,
            proof_of_space=proof_of_space(), farmer_puzzle_hash=std_hash(b"reward"),
            pool_target=PoolTarget(std_hash(b"pool"), uint32(0)),
            pool_signature=G2Element(), include_signature_source_data=False,
        )
        peer = SimpleNamespace(send_message=AsyncMock(), peer_node_id=std_hash(b"candidate-farmer"))
        with patch.object(node_api, "verify_and_get_quality_string", return_value=QUALITY), patch.object(
            node_api, "create_unfinished_block", return_value=SimpleNamespace(
                foliage=genesis.foliage, is_transaction_block=lambda: False),
        ) as create:
            await api.declare_proof_of_space(request, peer)
        chain.get_farmer_difficulty_coeff.assert_awaited_once_with(PK, parent.height)
        expected_required = calculate_iterations_quality(
            CONSTANTS.DIFFICULTY_CONSTANT_FACTOR, QUALITY, 32, uint64(7),
            Decimal("0.5"), request.challenge_chain_sp,
        )
        self.assertEqual(create.call_args.args[5], calculate_ip_iters(
            CONSTANTS, CONSTANTS.SUB_SLOT_ITERS_STARTING, request.signage_point_index, expected_required,
        ))


if __name__ == "__main__":
    unittest.main()
