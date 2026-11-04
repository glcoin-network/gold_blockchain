from __future__ import annotations

from decimal import Decimal
from typing import Optional

from gold_rs import G2Element

from chia.consensus.block_record import BlockRecord
from chia.consensus.blockchain_interface import BlockchainInterface
from chia.consensus.constants import ConsensusConstants
from chia.consensus.difficulty_adjustment import get_next_sub_slot_iters_and_difficulty
from chia.consensus.pot_iterations import (
    calculate_ip_iters,
    calculate_iterations_quality,
    calculate_sp_iters,
    is_overflow_block,
)
from chia.full_node.full_node_store import FullNodeStore
from chia.types.blockchain_format.proof_of_space import verify_and_get_quality_string
from chia.types.unfinished_block import UnfinishedBlock
from chia.util.ints import uint32, uint128
from chia.util.prev_transaction_block import get_prev_transaction_block


def refresh_candidate_iterations(
    constants: ConsensusConstants,
    blocks: BlockchainInterface,
    store: FullNodeStore,
    candidate: UnfinishedBlock,
    parent: BlockRecord,
    coefficient: Decimal,
) -> Optional[UnfinishedBlock]:
    """Rebuild still-pending local work; the caller must obtain a new plot signature."""
    if parent.header_hash == candidate.prev_header_hash or parent.total_iters >= candidate.total_iters:
        return None

    ancestor: Optional[BlockRecord] = parent
    for _ in range(50):
        if ancestor is None or ancestor.header_hash == candidate.prev_header_hash:
            break
        ancestor = blocks.try_block_record(ancestor.prev_hash)
    if ancestor is None or ancestor.header_hash != candidate.prev_header_hash:
        return None

    reward = candidate.reward_chain_block
    pos_challenge = reward.pos_ss_cc_challenge_hash
    if pos_challenge == constants.GENESIS_CHALLENGE:
        slot_start = uint128(0)
        slot_index = 0
    else:
        slot = store.get_sub_slot(pos_challenge)
        if slot is None:
            return None
        _, slot_index, slot_start = slot

    overflow = is_overflow_block(constants, reward.signage_point_index)
    ip_challenge = pos_challenge
    if overflow:
        # The next slot must exist before its infusion context can be refreshed.
        if slot_index + 1 >= len(store.finished_sub_slots):
            return None
        ip_slot = store.finished_sub_slots[slot_index + 1][0]
        if ip_slot is None:
            return None
        ip_challenge = ip_slot.challenge_chain.get_hash()
    finished_slots = store.get_finished_sub_slots(blocks, parent, ip_challenge)
    if finished_slots is None:
        return None
    sub_slot_iters, difficulty = get_next_sub_slot_iters_and_difficulty(
        constants, len(finished_slots) > 0, parent, blocks
    )
    sp_iters = calculate_sp_iters(constants, sub_slot_iters, reward.signage_point_index)
    if parent.total_iters < slot_start + sp_iters:
        # A block before the SP changes its reward-chain challenge; its old SP
        # signatures cannot be reused.
        return None
    cc_sp = pos_challenge if reward.challenge_chain_sp_vdf is None else reward.challenge_chain_sp_vdf.output.get_hash()
    quality = verify_and_get_quality_string(
        reward.proof_of_space, constants, pos_challenge, cc_sp, height=uint32(parent.height + 1)
    )
    if quality is None:
        return None
    required_iters = calculate_iterations_quality(
        constants.DIFFICULTY_CONSTANT_FACTOR, quality, reward.proof_of_space.size, difficulty, coefficient, cc_sp
    )
    ip_iters = calculate_ip_iters(constants, sub_slot_iters, reward.signage_point_index, required_iters)
    total_iters = uint128(slot_start + ip_iters + (sub_slot_iters if overflow else 0))
    if total_iters == candidate.total_iters or total_iters <= parent.total_iters:
        return None

    is_transaction, previous_transaction = get_prev_transaction_block(parent, blocks, uint128(slot_start + sp_iters))
    if is_transaction and (
        candidate.foliage_transaction_block is None
        or candidate.foliage_transaction_block.prev_transaction_block_hash != previous_transaction.header_hash
    ):
        return None
    reward = reward.replace(total_iters=total_iters)
    data = candidate.foliage.foliage_block_data.replace(unfinished_reward_block_hash=reward.get_hash())
    # Use the same transaction-block decision as unfinished_block_to_full_block.
    # Reward destinations and SP signatures survive; the changed foliage must be signed again.
    return candidate.replace(
        finished_sub_slots=finished_slots,
        reward_chain_block=reward,
        foliage=candidate.foliage.replace(
            prev_block_hash=parent.header_hash,
            reward_block_hash=reward.get_hash(),
            foliage_block_data=data,
            foliage_block_data_signature=G2Element(),
            foliage_transaction_block_hash=candidate.foliage.foliage_transaction_block_hash if is_transaction else None,
            foliage_transaction_block_signature=(
                candidate.foliage.foliage_transaction_block_signature if is_transaction else None
            ),
        ),
        foliage_transaction_block=candidate.foliage_transaction_block if is_transaction else None,
        transactions_info=candidate.transactions_info if is_transaction else None,
        transactions_generator=candidate.transactions_generator if is_transaction else None,
        transactions_generator_ref_list=candidate.transactions_generator_ref_list if is_transaction else [],
    )
