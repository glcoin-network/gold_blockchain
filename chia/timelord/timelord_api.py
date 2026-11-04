from __future__ import annotations

import dataclasses
import logging
import time
from decimal import Decimal
from typing import Optional

from chia.protocols import timelord_protocol
from chia.rpc.rpc_server import StateChangedProtocol
from chia.server.ws_connection import WSGoldConnection
from chia.server.outbound_message import NodeType
from chia.timelord.iters_from_block import iters_from_block
from chia.timelord.timelord import Timelord
from chia.timelord.types import Chain, IterationType, StateType
from chia.util.api_decorators import api_request
from chia.util.ints import uint64
from chia.util.network import is_localhost

log = logging.getLogger(__name__)


class TimelordAPI:
    log: logging.Logger
    timelord: Timelord

    def __init__(self, timelord) -> None:
        self.log = logging.getLogger(__name__)
        self.timelord = timelord

    def ready(self) -> bool:
        return True

    def _set_state_changed_callback(self, callback: StateChangedProtocol) -> None:
        self.timelord.state_changed_callback = callback

    @staticmethod
    def _is_local_full_node(peer: WSGoldConnection) -> bool:
        return (
            not peer.closed
            and peer.is_outbound
            and peer.connection_type == NodeType.FULL_NODE
            and is_localhost(peer.peer_info.host)
        )

    @api_request(peer_required=True)
    async def new_peak_timelord(
        self, new_peak: timelord_protocol.NewPeakTimelord, peer: WSGoldConnection
    ) -> None:
        if self.timelord.last_state is None:
            return None
        async with self.timelord.lock:
            if self.timelord.bluebox_mode:
                return None
            self.timelord.max_allowed_inactivity_time = 60
            local_context = self._is_local_full_node(peer)

            if (
                self.timelord.last_state.peak is not None
                and self.timelord.last_state.peak.reward_chain_block == new_peak.reward_chain_block
            ):
                if local_context:
                    self.timelord.pending_local_infusion = None
                    self.timelord.confirmed_local_peak = (
                        dataclasses.replace(new_peak, previous_reward_challenges=list(new_peak.previous_reward_challenges)),
                        peer,
                    )
                self.timelord.pending_peak_recovery = None
                log.info("Skipping peak, already have.")
                self.timelord.state_changed("skipping_peak", {"height": new_peak.reward_chain_block.height})
                return None

            pending = self.timelord.pending_peak_recovery
            if pending is not None and new_peak.reward_chain_block.weight > pending[0].reward_chain_block.weight:
                peak_hash = new_peak.reward_chain_block.get_hash()
                if any(
                    challenge == peak_hash
                    and total_iters == new_peak.reward_chain_block.total_iters
                    for challenge, total_iters in self.timelord.last_state.reward_challenge_cache
                ):
                    self.timelord.pending_peak_recovery = None

            # if there is a heavier unfinished block from a diff chain, skip
            if self.timelord.last_state.state_type == StateType.PEAK:
                for unf_block in self.timelord.unfinished_blocks:
                    if unf_block.reward_chain_block.total_iters > new_peak.reward_chain_block.total_iters:
                        found = False
                        for rc, total_iters in new_peak.previous_reward_challenges:
                            if rc == unf_block.rc_prev:
                                found = True
                                break

                        if not found:
                            self.timelord._queue_peak_recovery(new_peak, peer)
                            log.info(
                                "there is a heavier unfinished block that does not belong to this chain- skip peak"
                            )
                            return None

            self.timelord.pending_peak_recovery = None
            if local_context:
                self.timelord.pending_local_infusion = None
                self.timelord.confirmed_local_peak = (
                    dataclasses.replace(new_peak, previous_reward_challenges=list(new_peak.previous_reward_challenges)),
                    peer,
                )
            if new_peak.reward_chain_block.weight > self.timelord.last_state.get_weight():
                log.info("Not skipping peak, don't have. Maybe we are not the fastest timelord")
                log.info(
                    f"New peak: height: {new_peak.reward_chain_block.height} weight: "
                    f"{new_peak.reward_chain_block.weight} "
                )
                self.timelord.new_peak = new_peak
                self.timelord.state_changed("new_peak", {"height": new_peak.reward_chain_block.height})
            else:
                log.warning("block that we don't have, changing to it.")
                self.timelord.new_peak = new_peak
                self.timelord.state_changed("new_peak", {"height": new_peak.reward_chain_block.height})

    @api_request(peer_required=True)
    async def new_unfinished_block_timelord(
        self, new_unfinished_block: timelord_protocol.NewUnfinishedBlockTimelord, peer: WSGoldConnection
    ):
        if self.timelord.last_state is None:
            return None
        async with self.timelord.lock:
            if self.timelord.bluebox_mode:
                return None
            reward_hash = new_unfinished_block.reward_chain_block.get_hash()
            local_context = self._is_local_full_node(peer)
            try:
                coefficient = Decimal(new_unfinished_block.difficulty_coeff)
                if not coefficient.is_finite() or coefficient <= 0:
                    return None
                if local_context:
                    self.timelord.candidate_coefficients.put(reward_hash, new_unfinished_block.difficulty_coeff)
                else:
                    current = self.timelord.candidate_coefficients.get(reward_hash)
                    if current is not None:
                        new_unfinished_block = dataclasses.replace(new_unfinished_block, difficulty_coeff=current)
                existing = [
                    block for block in self.timelord.unfinished_blocks + self.timelord.overflow_blocks
                    if block.reward_chain_block.get_hash() == reward_hash
                ]
                if existing:
                    if all(block.difficulty_coeff == new_unfinished_block.difficulty_coeff for block in existing):
                        return None
                    previous = existing[0]
                    try:
                        _, old_ip = iters_from_block(
                            self.timelord.constants, previous.reward_chain_block, previous.sub_slot_iters,
                            previous.difficulty, self.timelord.get_height(), Decimal(previous.difficulty_coeff),
                        )
                        _, new_ip = iters_from_block(
                            self.timelord.constants, previous.reward_chain_block, previous.sub_slot_iters,
                            previous.difficulty, self.timelord.get_height(), Decimal(new_unfinished_block.difficulty_coeff),
                        )
                        same_iterations = old_ip == new_ip
                    except ValueError:
                        same_iterations = False
                    if same_iterations:
                        # Keep completed work when the coefficient rounds to the
                        # same IP, independently of the current slot's origin.
                        for pending in (self.timelord.unfinished_blocks, self.timelord.overflow_blocks):
                            for index, block in enumerate(pending):
                                if block.reward_chain_block.get_hash() == reward_hash:
                                    pending[index] = dataclasses.replace(
                                        block, difficulty_coeff=new_unfinished_block.difficulty_coeff
                                    )
                        return None
                    self.timelord._retire_candidate(reward_hash)
                    log.warning(
                        "Retired stale candidate %s after stake coefficient update: %s -> %s; "
                        "signed total iters %s no longer match the infusion context",
                        reward_hash.hex(), previous.difficulty_coeff, new_unfinished_block.difficulty_coeff,
                        previous.reward_chain_block.total_iters,
                    )
                    return None
                sp_iters, ip_iters = iters_from_block(
                    self.timelord.constants,
                    new_unfinished_block.reward_chain_block,
                    self.timelord.last_state.get_sub_slot_iters(),
                    self.timelord.last_state.get_difficulty(),
                    self.timelord.get_height(),
                    Decimal(new_unfinished_block.difficulty_coeff),
                )
            except Exception as error:
                log.warning("Cannot schedule candidate %s: invalid iteration context: %s", reward_hash.hex(), error)
                return None
            last_ip_iters = self.timelord.last_state.get_last_ip()
            if sp_iters > ip_iters:
                self.timelord.overflow_blocks.append(new_unfinished_block)
                log.debug(f"Overflow unfinished block, total {self.timelord.total_unfinished}")
            elif ip_iters > last_ip_iters:
                new_block_iters: Optional[uint64] = self.timelord._can_infuse_unfinished_block(new_unfinished_block)
                if new_block_iters:
                    self.timelord.unfinished_blocks.append(new_unfinished_block)
                    for chain in [Chain.REWARD_CHAIN, Chain.CHALLENGE_CHAIN]:
                        self.timelord.iters_to_submit[chain].append(new_block_iters)
                    if self.timelord.last_state.get_deficit() < self.timelord.constants.MIN_BLOCKS_PER_CHALLENGE_BLOCK:
                        self.timelord.iters_to_submit[Chain.INFUSED_CHALLENGE_CHAIN].append(new_block_iters)
                    self.timelord.iteration_to_proof_type[new_block_iters] = IterationType.INFUSION_POINT
                    self.timelord.total_unfinished += 1
                    log.debug(f"Non-overflow unfinished block, total {self.timelord.total_unfinished}")

    @api_request()
    async def request_compact_proof_of_time(self, vdf_info: timelord_protocol.RequestCompactProofOfTime):
        async with self.timelord.lock:
            if not self.timelord.bluebox_mode:
                return None
            now = time.time()
            # work older than 5s can safely be assumed to be from the previous batch, and needs to be cleared
            while self.timelord.pending_bluebox_info and (now - self.timelord.pending_bluebox_info[0][0] > 5):
                del self.timelord.pending_bluebox_info[0]
            self.timelord.pending_bluebox_info.append((now, vdf_info))
