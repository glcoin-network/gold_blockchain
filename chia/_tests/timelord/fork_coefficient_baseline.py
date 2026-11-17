"""Frozen pre-repair coefficient methods for before/after regression comparisons."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from gold_rs import G1Element

from chia.consensus.block_record import BlockRecord
from chia.consensus.blockchain import Blockchain
from chia.consensus.coinbase import create_puzzlehash_for_pk
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint32, uint64, uint128


class LegacyBlockchain(Blockchain):
    async def get_network_space(self, newer: bytes32, older: bytes32) -> uint128:
        newer_block = await self.block_store.get_block_record(newer)
        if newer_block is None:
            raise ValueError("Newer block not found")
        older_block = await self.block_store.get_block_record(older)
        if older_block is None:
            raise ValueError("Older block not found")
        delta_weight = newer_block.weight - older_block.weight
        delta_iters = newer_block.total_iters - older_block.total_iters
        weight_div_iters = delta_weight / delta_iters
        additional_difficulty_constant = self.constants.DIFFICULTY_CONSTANT_FACTOR
        eligible_plots_filter_multiplier = 2 ** self.constants.NUMBER_ZERO_BITS_PLOT_FILTER
        network_space_bytes_estimate = (
            0.762
            * weight_div_iters
            * additional_difficulty_constant
            * eligible_plots_filter_multiplier
        )
        return uint128(int(network_space_bytes_estimate))

    async def get_peak_network_space(self, block_range: int, peak: Optional[BlockRecord]) -> uint128:
        if peak is not None and peak.height > 1:
            older_header_hash = self.height_to_hash(uint32(max(1, peak.height - block_range)))
            assert older_header_hash is not None
            return await self.get_network_space(peak.header_hash, older_header_hash)
        return uint128(0)

    async def get_peak_farmer_staking(self, farmer_public_key: G1Element, peak: Optional[BlockRecord]) -> uint64:
        ph = create_puzzlehash_for_pk(farmer_public_key)
        coins = await self.coin_store.get_unspent_coins_before_height(ph, peak.height if peak is not None else 0)
        return uint64(sum(coin.coin.amount for coin in coins))

    async def get_farmer_difficulty_coeff(
        self,
        farmer_public_key: G1Element,
        height: Optional[uint32] = None,
        blocks: Optional[uint64] = None,
    ) -> Decimal:
        block_range = self.constants.STAKING_ESTIMATE_BLOCK_RANGE
        peak: Optional[BlockRecord] = None
        peak_height = height
        if peak_height is None:
            peak_height = self._peak_height
        if peak_height is not None:
            if peak_height == 0:
                return Decimal(20)
            header_hash = self.height_to_hash(peak_height)
            peak = await self.block_store.get_block_record(header_hash)
        if blocks is None:
            curr: Optional[BlockRecord] = peak
            begin_height = max((curr.height if curr is not None else 0) - block_range, 1)
            blocks = 0
            while curr is not None and curr.height > begin_height:
                if curr.farmer_public_key == farmer_public_key:
                    blocks += 1
                curr = self.try_block_record(curr.prev_hash)
        network_space = await self.get_peak_network_space(block_range, peak)
        staking = await self.get_peak_farmer_staking(farmer_public_key, peak)
        minimal_staking = Decimal(str(network_space)) / (block_range * 100)
        coeff = Decimal(20)
        if 0 < minimal_staking <= staking:
            if blocks == 0:
                coeff = Decimal(1)
            else:
                space = uint128(int(network_space * blocks / block_range))
                if space == 0:
                    coeff = Decimal(1)
                elif staking >= space:
                    coeff = Decimal("0.5") + Decimal(1) / (Decimal(str(staking)) / space + 1)
                else:
                    coeff = Decimal("0.05") + Decimal(1) / (Decimal(str(staking)) / space + Decimal("0.05"))
        return coeff
