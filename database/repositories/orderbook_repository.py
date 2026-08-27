from typing import Dict, List, Optional, Sequence

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import MarketData


class OrderBookRepository:
    """Repository for order book snapshot data access."""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_spread_samples(
        self,
        pair: Optional[str] = None,
        connector: Optional[str] = None,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[Dict]:
        """
        Get raw spread samples with filtering and pagination.
        
        Args:
            pair: Optional trading pair filter
            connector: Optional connector filter
            start_timestamp: Optional start time filter (milliseconds)
            end_timestamp: Optional end time filter (milliseconds)
            limit: Maximum number of records to return
            offset: Pagination offset
            
        Returns:
            List of spread sample dictionaries
        """
        query = select(
            MarketData.trading_pair.label("pair"),
            MarketData.exchange.label("connector"),
            MarketData.timestamp,
            MarketData.best_bid.label("bid"),
            MarketData.best_ask.label("ask"),
            MarketData.mid_price.label("mid"),
            MarketData.spread,
        )
        
        # Apply filters
        if pair:
            query = query.where(MarketData.trading_pair == pair)
        
        if connector:
            query = query.where(MarketData.exchange == connector)
        
        if start_timestamp:
            query = query.where(MarketData.timestamp >= start_timestamp)
        
        if end_timestamp:
            query = query.where(MarketData.timestamp <= end_timestamp)
        
        # Order by timestamp descending (most recent first)
        query = query.order_by(desc(MarketData.timestamp))
        
        # Apply pagination
        if limit is not None:
            query = query.limit(limit).offset(offset)
        
        # Execute query
        result = await self.session.execute(query)
        return [self._row_to_dict(row) for row in result.mappings().all()]

    async def get_spread_averages(
        self,
        pairs: Optional[Sequence[str]] = None,
        connectors: Optional[Sequence[str]] = None,
        start_timestamp: Optional[int] = None,
    ) -> List[Dict]:
        query = (
            select(
                MarketData.trading_pair.label("pair"),
                MarketData.exchange.label("connector"),
                func.avg(MarketData.spread).label("avg_spread"),
                func.min(MarketData.spread).label("min_spread"),
                func.max(MarketData.spread).label("max_spread"),
                func.count(MarketData.spread).label("sample_count"),
            )
            .where(MarketData.spread.is_not(None))
            .group_by(MarketData.trading_pair, MarketData.exchange)
            .order_by(MarketData.exchange, MarketData.trading_pair)
        )

        if pairs:
            query = query.where(MarketData.trading_pair.in_(pairs))

        if connectors:
            query = query.where(MarketData.exchange.in_(connectors))

        if start_timestamp:
            query = query.where(MarketData.timestamp >= start_timestamp)

        result = await self.session.execute(query)
        return [
            {
                "pair": row["pair"],
                "connector": row["connector"],
                "avg_spread": round(float(row["avg_spread"]), 2),
                "min_spread": float(row["min_spread"]),
                "max_spread": float(row["max_spread"]),
                "sample_count": int(row["sample_count"]),
            }
            for row in result.mappings().all()
        ]

    async def count_spread_samples(
        self,
        pair: Optional[str] = None,
        connector: Optional[str] = None,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
    ) -> int:
        """
        Count total spread samples matching the given filters.

        Args:
            pair: Optional trading pair filter
            connector: Optional connector filter
            start_timestamp: Optional start time filter (milliseconds)
            end_timestamp: Optional end time filter (milliseconds)

        Returns:
            Total number of matching records
        """
        query = select(func.count()).select_from(MarketData)

        if pair:
            query = query.where(MarketData.trading_pair == pair)

        if connector:
            query = query.where(MarketData.exchange == connector)

        if start_timestamp:
            query = query.where(MarketData.timestamp >= start_timestamp)

        if end_timestamp:
            query = query.where(MarketData.timestamp <= end_timestamp)

        result = await self.session.execute(query)
        return result.scalar_one()

    def to_dict(self, sample: MarketData) -> Dict:
        """
        Convert MarketData model to dictionary format.
        
        Args:
            sample: MarketData object
            
        Returns:
            Dictionary representation
        """
        return {
            "pair": sample.trading_pair,
            "connector": sample.exchange,
            "timestamp": sample.timestamp,
            "bid": float(sample.best_bid) if sample.best_bid else None,
            "ask": float(sample.best_ask) if sample.best_ask else None,
            "mid": float(sample.mid_price) if sample.mid_price else None,
            "spread": float(sample.spread) if sample.spread else None
        }

    def _row_to_dict(self, row: Dict) -> Dict:
        return {
            "pair": row["pair"],
            "connector": row["connector"],
            "timestamp": row["timestamp"],
            "bid": float(row["bid"]) if row["bid"] is not None else None,
            "ask": float(row["ask"]) if row["ask"] is not None else None,
            "mid": float(row["mid"]) if row["mid"] is not None else None,
            "spread": float(row["spread"]) if row["spread"] is not None else None,
        }
