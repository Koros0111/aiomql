from dataclasses import dataclass, field, fields
import pickle
from pathlib import Path
from datetime import datetime, UTC
from logging import getLogger
from typing import Sequence, NamedTuple

import MetaTrader5
from numpy import ndarray

from ...core.meta_trader import MetaTrader
from ...core.config import Config
from ...core.constants import TimeFrame
from ...core.task_queue import TaskQueue, QueueItem
from ..._utils import backoff_decorator

logger = getLogger(__name__)


class Cursor(NamedTuple):
    index: int
    time: int


@dataclass
class BackTestData:
    name: str = ''
    terminal: dict[str, [str | int | bool | float]] = field(default_factory=dict)
    version: tuple[int, int, str] = (0, 0, '')
    account: dict = field(default_factory=dict)
    symbols: dict[str, dict] = field(default_factory=dict)
    ticks: dict[str, ndarray] = field(default_factory=dict)
    rates: dict[str, dict[int, ndarray]] = field(default_factory=dict)
    span: range = range(0)
    range: range = range(0)
    orders: dict[int, dict] = field(default_factory=lambda: {})
    deals: dict[int, dict] = field(default_factory=lambda: {})
    positions: dict[int, dict] = field(default_factory=lambda: {})
    open_positions: set[int, ...] = field(default_factory=lambda: set())
    cursor: Cursor = None
    margins: dict[int, float] = field(default_factory=lambda: {})
    fully_loaded: bool = True

    def __str__(self):
        return f"{self.name}"

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"

    def set_attrs(self, **kwargs):
        [setattr(self, k, v) for k, v in kwargs.items() if k in self.fields]

    @property
    def fields(self):
        return [f.name for f in fields(self)]


class GetData:
    data: BackTestData

    def __init__(self, *, start: datetime, end: datetime, symbols: Sequence[str],
                 timeframes: Sequence[TimeFrame], name: str = ''):
        """"""
        self.config = Config()
        self.start = start.astimezone(tz=UTC)
        self.end = end.astimezone(tz=UTC)
        self.symbols = set(symbols)
        self.timeframes = set(timeframes)
        self.name = name or f"{start:%d-%m-%y}_{end:%d-%m-%y}"
        span_start = int(self.start.timestamp())
        span_end = int(self.end.timestamp())
        self.range = range(0, span_end - span_start)
        self.span = range(span_start, span_end)
        self.data = BackTestData(name=self.name, span=self.span, range=self.range)
        self.mt5 = MetaTrader()
        self.task_queue = TaskQueue(workers=500, mode='finite', on_exit='cancel')

    @classmethod
    def pickle_data(cls, *, data: BackTestData, name: str | Path):
        """"""
        try:
            with open(name, 'wb') as fo:
                pickle.dump(data, fo, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as err:
            logger.error(f"Error in dump_data: {err}")

    @classmethod
    def load_data(cls, *, name: str | Path) -> BackTestData:
        """"""
        try:
            with open(name, 'rb') as fo:
                data = pickle.load(fo)
                return data
        except Exception as err:
            logger.error(f"Error: {err}")

    def save_data(self, *, name: str | Path = ''):
        name = name or (self.name + '.pkl' if not self.name.endswith('.pkl') else self.name)
        name = Path(self.config.backtest_dir) / name if not isinstance(name, Path) else name
        with open(name, 'wb') as fo:
            pickle.dump(self.data, fo, protocol=pickle.HIGHEST_PROTOCOL)

    async def get_data(self, workers: int = None):
        """"""
        if workers:
            self.task_queue.workers = workers

        q_items = [QueueItem(self.get_symbols_rates),
                   QueueItem(self.get_symbols_ticks),
                   QueueItem(self.get_symbols_info),
                   ]

        [self.task_queue.add(item=item, priority=0, must_complete=True) for item in q_items]

        if not self.data.account:
            self.task_queue.add(item=QueueItem(self.get_account_info), must_complete=True)

        if not self.data.terminal:
            self.task_queue.add(item=QueueItem(self.get_terminal_info), must_complete=True)

        if not self.data.version:
            self.task_queue.add(item=QueueItem(self.get_version), must_complete=True)

        await self.task_queue.run()

        if self.data.fully_loaded is False:
            logger.warning("Data not fully loaded")
            self.data = BackTestData(name=self.name, span=self.span, range=self.range, fully_loaded=False)

    async def get_terminal_info(self):
        """"""
        terminal = await self.mt5.terminal_info()
        if terminal is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        terminal = terminal._asdict()
        self.data.set_attrs(terminal=terminal)

    async def get_version(self):
        """"""
        version = await self.mt5.version()
        if version is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        self.data.set_attrs(version=version)

    @backoff_decorator
    async def get_account_info(self):
        """"""
        res = await self.mt5.account_info()
        if res is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        res = res._asdict()
        self.data.set_attrs(account=res)

    async def get_symbols_info(self):
        """"""
        [self.task_queue.add(item=QueueItem(self.get_symbol_info, symbol=symbol))
         for symbol in self.symbols if self.data.symbols.get(symbol) is None]

    async def get_symbols_ticks(self):
        """"""
        [self.task_queue.add(item=QueueItem(self.get_symbol_ticks, symbol=symbol))
         for symbol in self.symbols if self.data.ticks.get(symbol) is None]

    async def get_symbols_rates(self):
        """"""
        [self.task_queue.add(item=QueueItem(self.get_symbol_rates, symbol=symbol, timeframe=timeframe), priority=4)
                  for symbol in self.symbols for timeframe in self.timeframes
         if self.data.rates.get(symbol, {}).get(timeframe) is None]

    @backoff_decorator
    async def get_symbol_info(self, *, symbol: str):
        """"""
        res = await self.mt5.symbol_info(symbol)
        if res is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        self.data.symbols[symbol] = res._asdict()

    @backoff_decorator
    async def get_symbol_ticks(self, *, symbol: str):
        """"""
        res = await self.mt5.copy_ticks_range(symbol, self.start, self.end, MetaTrader5.COPY_TICKS_ALL)
        if res is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        self.data.ticks[symbol] = res

    @backoff_decorator
    async def get_symbol_rates(self, *, symbol: str, timeframe: TimeFrame):
        """"""
        res = await self.mt5.copy_rates_range(symbol, timeframe, self.start, self.end)
        if res is None:
            self.data.fully_loaded = False
            self.task_queue.stop_queue()
        self.data.rates.setdefault(symbol, {})[int(timeframe)] = res
