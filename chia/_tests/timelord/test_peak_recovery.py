"""Recovery regressions, runnable with stdlib or the installed Gold dependencies."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[3]
REAL_IMPORTS = os.environ.get("GOLD_TEST_REAL_IMPORTS") == "1"
TIMELORD_SOURCE = ROOT / "chia/timelord/timelord.py"
API_SOURCE = ROOT / "chia/timelord/timelord_api.py"


def load_source_methods():
    namespace = {
        "__name__": "peak_recovery_test",
        "Enum": Enum,
        "logging": logging,
        "log": logging.getLogger("peak_recovery_test"),
        "Decimal": Decimal,
        "uint8": int,
        "uint64": int,
    }

    def api_request(**metadata):
        def decorate(function):
            function.test_api_metadata = metadata
            return function

        return decorate

    namespace["api_request"] = api_request
    sources = [
        (ROOT / "chia/timelord/types.py", None),
        (ROOT / "chia/server/outbound_message.py", {"NodeType"}),
        (ROOT / "chia/util/network.py", {"is_localhost"}),
        (
            TIMELORD_SOURCE,
            {"Timelord"},
        ),
        (API_SOURCE, {"TimelordAPI"}),
    ]
    for path, names in sources:
        nodes = []
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                continue
            if names is not None and node.name not in names:
                continue
            if node.name == "Timelord":
                methods = {
                    "_queue_peak_recovery", "_check_for_peak_recovery", "_reset_chains",
                    "_handle_new_peak", "_manage_chains",
                }
                node.body = [
                    item
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in methods
                    or isinstance(item, ast.AnnAssign) and item.target.id == "PEAK_RECOVERY_TIMEOUT"
                ]
            nodes.append(node)
        if path.name == "outbound_message.py":
            namespace["IntEnum"] = __import__("enum").IntEnum
        module = ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                *nodes,
            ],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


if REAL_IMPORTS:
    from chia.timelord import timelord as timelord_module
    from chia.timelord import timelord_api as api_module
    from chia.timelord.types import Chain, IterationType, StateType
    from chia.server.outbound_message import NodeType

    Timelord = timelord_module.Timelord
    TimelordAPI = api_module.TimelordAPI
else:
    source_namespace = load_source_methods()
    Timelord = source_namespace["Timelord"]
    TimelordAPI = source_namespace["TimelordAPI"]
    Chain = source_namespace["Chain"]
    IterationType = source_namespace["IterationType"]
    StateType = source_namespace["StateType"]
    NodeType = source_namespace["NodeType"]


@dataclass(frozen=True)
class Block:
    height: int
    weight: int
    total_iters: int
    name: str

    def get_hash(self):
        return self.name

    def get_unfinished(self):
        return self


def peak(block):
    return SimpleNamespace(reward_chain_block=block, previous_reward_challenges=[("ancestor", 100_000)])


class PeakRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        clock = SimpleNamespace(monotonic=lambda: self.now, time=lambda: self.now)
        if REAL_IMPORTS:
            self.clock_patch = patch.object(timelord_module, "time", clock)
            self.clock_patch.start()
            self.addCleanup(self.clock_patch.stop)
        else:
            source_namespace["time"] = clock
        self.confirmed = peak(Block(10, 1000, 100_000, "ancestor"))
        self.speculative = peak(Block(11, 1100, 110_000, "speculative"))
        self.state = SimpleNamespace(
            state_type=StateType.PEAK,
            peak=self.speculative,
            reward_challenge_cache=[("ancestor", 100_000), ("speculative", 110_000)],
            get_height=lambda: self.state.peak.reward_chain_block.height,
            get_weight=lambda: self.state.peak.reward_chain_block.weight,
            get_total_iters=lambda: self.state.peak.reward_chain_block.total_iters,
            get_last_ip=lambda: 100,
            get_sub_slot_iters=lambda: 51200,
            get_deficit=lambda: 16,
        )
        self.state.set_state = lambda value: setattr(self.state, "peak", value)
        self.timelord = Timelord.__new__(Timelord)
        self.timelord.last_state = self.state
        self.timelord.lock = asyncio.Lock()
        self.timelord.bluebox_mode = False
        self.timelord.pending_peak_recovery = None
        self.timelord.new_peak = None
        self.timelord.max_allowed_inactivity_time = 60
        self.timelord.unfinished_blocks = [
            SimpleNamespace(reward_chain_block=Block(12, 1200, 120_000, "future"), rc_prev="speculative")
        ]
        self.timelord.overflow_blocks = [object()]
        self.timelord.state_changed = Mock()
        self.peer = SimpleNamespace(
            closed=False,
            is_outbound=True,
            connection_type=NodeType.FULL_NODE,
            peer_info=SimpleNamespace(host="127.0.0.1"),
        )
        self.api = TimelordAPI(self.timelord)

    async def send_peak(self, value=None, peer=None):
        await self.api.new_peak_timelord(value or self.confirmed, peer or self.peer)

    async def test_rejected_local_infusion_recovers_after_grace(self):
        await self.send_peak()
        self.assertIsNone(self.timelord.new_peak)
        self.assertIsNotNone(self.timelord.pending_peak_recovery)
        self.now += 14.999
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.new_peak)
        self.assertTrue(self.timelord.unfinished_blocks)
        self.now += 0.001
        self.timelord._check_for_peak_recovery()
        self.assertIs(self.timelord.new_peak, self.confirmed)
        self.assertEqual(self.timelord.unfinished_blocks, [])
        self.assertEqual(self.timelord.overflow_blocks, [])
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_repeated_correction_does_not_extend_deadline(self):
        await self.send_peak()
        self.now += 10
        await self.send_peak()
        self.assertEqual(self.timelord.pending_peak_recovery[2], 1000)
        self.now += 5
        self.timelord._check_for_peak_recovery()
        self.assertIs(self.timelord.new_peak, self.confirmed)

    async def test_matching_confirmation_cancels_recovery(self):
        await self.send_peak()
        confirmation = peak(self.speculative.reward_chain_block)
        await self.send_peak(confirmation)
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.new_peak)
        self.assertTrue(self.timelord.unfinished_blocks)

    async def test_confirmation_of_intermediate_ancestor_cancels_obsolete_recovery(self):
        await self.send_peak()
        self.state.peak = peak(Block(14, 1400, 140_000, "ahead"))
        self.state.reward_challenge_cache.append(("progress", 120_000))
        self.timelord.unfinished_blocks = [
            SimpleNamespace(reward_chain_block=Block(15, 1500, 150_000, "future"), rc_prev="ahead")
        ]
        await self.send_peak(peak(Block(12, 1200, 120_000, "progress")))
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.assertIsNone(self.timelord.new_peak)
        self.assertTrue(self.timelord.unfinished_blocks)

    async def test_heavier_confirmed_peak_cancels_recovery(self):
        await self.send_peak()
        incoming = peak(Block(12, 1200, 130_000, "confirmed-new"))
        await self.send_peak(incoming)
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIs(self.timelord.new_peak, incoming)
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.assertTrue(self.timelord.unfinished_blocks)

    async def test_recovery_restarts_chains_and_invalidates_old_proofs(self):
        await self.send_peak()
        self.timelord.constants = SimpleNamespace(NUM_SPS_SUB_SLOT=64, MIN_BLOCKS_PER_CHALLENGE_BLOCK=16)
        self.timelord.total_unfinished = 1
        self.timelord.total_infused = 0
        self.timelord.chain_type_to_stream = {chain: object() for chain in list(Chain)[:3]}
        self.timelord._stop_chain = AsyncMock()
        self.timelord.num_resets = 7
        self.timelord.iters_to_submit = {}
        self.timelord.iters_submitted = {}
        self.timelord.iters_finished = {123}
        self.timelord.proofs_finished = [object()]
        self.now += 15
        self.timelord._check_for_peak_recovery()
        await self.timelord._handle_new_peak()
        self.assertIs(self.state.peak, self.confirmed)
        self.assertIsNone(self.timelord.new_peak)
        self.assertEqual(self.timelord.num_resets, 8)
        self.assertEqual(self.timelord.proofs_finished, [])
        self.assertEqual(self.timelord.iters_finished, set())
        self.assertEqual(self.timelord._stop_chain.await_count, 3)
        self.assertIn(51100, self.timelord.iters_to_submit[Chain.CHALLENGE_CHAIN])
        self.assertEqual(self.timelord.iteration_to_proof_type[51100], IterationType.END_OF_SUBSLOT)

    async def test_remote_and_inbound_peers_cannot_queue_recovery(self):
        for host, outbound, node_type, closed in [
            ("203.0.113.1", True, NodeType.FULL_NODE, False),
            ("127.0.0.1", False, NodeType.FULL_NODE, False),
            ("127.0.0.1", True, NodeType.FARMER, False),
            ("127.0.0.1", True, NodeType.FULL_NODE, True),
        ]:
            with self.subTest(host=host, outbound=outbound, node_type=node_type, closed=closed):
                self.peer.peer_info.host = host
                self.peer.is_outbound = outbound
                self.peer.connection_type = node_type
                self.peer.closed = closed
                await self.send_peak()
                self.assertIsNone(self.timelord.pending_peak_recovery)
                self.assertIsNone(self.timelord.new_peak)
                self.assertTrue(self.timelord.unfinished_blocks)

    async def test_ipv6_loopback_can_queue_recovery(self):
        self.peer.peer_info.host = "::1"
        await self.send_peak()
        self.assertIsNotNone(self.timelord.pending_peak_recovery)

    async def test_unknown_ancestor_cannot_queue_recovery(self):
        self.state.reward_challenge_cache = [("other-branch", 100_000)]
        await self.send_peak()
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_wrong_ancestor_iters_cannot_queue_recovery(self):
        self.state.reward_challenge_cache = [("ancestor", 99_999)]
        await self.send_peak()
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_distant_stale_peak_cannot_queue_recovery(self):
        stale = peak(Block(9, 900, 90_000, "old"))
        self.state.reward_challenge_cache.append(("old", 90_000))
        await self.send_peak(stale)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_non_lighter_peak_cannot_queue_recovery(self):
        correction = peak(Block(10, 1100, 100_000, "ancestor"))
        await self.send_peak(correction)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_disconnect_cancels_pending_recovery(self):
        await self.send_peak()
        self.peer.closed = True
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.assertIsNone(self.timelord.new_peak)
        self.assertTrue(self.timelord.unfinished_blocks)

    async def test_changed_branch_cancels_pending_recovery(self):
        await self.send_peak()
        self.state.reward_challenge_cache.clear()
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.new_peak)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_bluebox_does_not_queue_recovery(self):
        self.timelord.bluebox_mode = True
        await self.send_peak()
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.assertTrue(self.timelord.unfinished_blocks)

    async def test_bluebox_cancels_existing_recovery(self):
        await self.send_peak()
        self.timelord.bluebox_mode = True
        self.now += 20
        self.timelord._check_for_peak_recovery()
        self.assertIsNone(self.timelord.new_peak)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_no_pending_recovery_does_not_clear_tasks(self):
        self.timelord._check_for_peak_recovery()
        self.assertTrue(self.timelord.unfinished_blocks)
        self.assertTrue(self.timelord.overflow_blocks)

    async def test_normal_peak_without_conflict_keeps_existing_behavior(self):
        self.timelord.unfinished_blocks.clear()
        await self.send_peak()
        self.assertIs(self.timelord.new_peak, self.confirmed)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    async def test_end_of_subslot_does_not_queue_recovery(self):
        self.state.state_type = StateType.END_OF_SUB_SLOT
        await self.send_peak()
        self.assertIs(self.timelord.new_peak, self.confirmed)
        self.assertIsNone(self.timelord.pending_peak_recovery)

    def test_peer_context_is_requested_without_changing_message(self):
        tree = ast.parse(API_SOURCE.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TimelordAPI")
        handler = next(node for node in cls.body if getattr(node, "name", None) == "new_peak_timelord")
        self.assertTrue(
            any(keyword.arg == "peer_required" and keyword.value.value is True for keyword in handler.decorator_list[0].keywords)
        )

    async def test_main_loop_applies_recovery_without_waiting_for_end_of_slot(self):
        await self.send_peak()
        self.now += 15
        self.timelord._shut_down = False
        self.timelord.total_unfinished = 0
        self.timelord._reset_chains = AsyncMock()
        self.timelord._handle_failures = AsyncMock()
        self.timelord._submit_iterations = AsyncMock()
        self.timelord.iters_submitted = {Chain.REWARD_CHAIN: []}

        async def finish_iteration():
            self.timelord._shut_down = True

        self.timelord._map_chains_with_vdf_clients = AsyncMock(side_effect=finish_iteration)
        async_runtime = SimpleNamespace(sleep=AsyncMock())
        if REAL_IMPORTS:
            with patch.object(timelord_module, "asyncio", async_runtime):
                await self.timelord._manage_chains()
        else:
            source_namespace["asyncio"] = async_runtime
            await self.timelord._manage_chains()
        self.assertIs(self.state.peak, self.confirmed)
        self.assertIsNone(self.timelord.new_peak)
        self.assertIsNone(self.timelord.pending_peak_recovery)
        self.assertEqual(self.timelord._reset_chains.await_count, 2)


if __name__ == "__main__":
    print("Mode:", "installed dependencies" if REAL_IMPORTS else "source-extracted unit tests")
    unittest.main(verbosity=2)
