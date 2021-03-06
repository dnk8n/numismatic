import logging
from configparser import ConfigParser
from appdirs import user_config_dir
from pathlib import Path
import os
import click
from itertools import chain, product
from collections import namedtuple
import asyncio
from streamz import Stream
import attr


logger = logging.getLogger(__name__)


config = ConfigParser()
# TODO: better define which files will be supported or use click-config pkg
for config_file in [Path(user_config_dir()) / 'numismatic.ini',
                    Path(os.environ['HOME']) / '.coinrc']:
    if config_file.exists():
        config.read(config_file)


DEFAULT_ASSETS = ['BTC']
DEFAULT_CURRENCIES = ['USD']
ENVVAR_PREFIX = 'NUMISMATIC'

pass_state = click.make_pass_decorator(dict, ensure=True)

@click.group(chain=True)
@click.option('--feed', '-f', default='cryptocompare',
              type=click.Choice(['cryptocompare', 'luno']))
@click.option('--cache-dir', '-d', default=None)
@click.option('--requester', '-r', default='caching',
              type=click.Choice(['base', 'caching']))
@click.option('--log-level', '-l', default='info', 
              type=click.Choice(['debug', 'info', 'warning', 'error',
                                 'critical']))
@pass_state
def coin(state, feed, cache_dir, requester, log_level):
    '''Numismatic Command Line Interface

    Examples:

        coin list

        coin info

        coin info -a ETH

        coin prices

        coin prices -a XMR,ZEC -c EUR,ZAR

        coin -f luno prices -a XBT -c ZAR

        NUMISMATIC_CURRENCIES=ZAR coin prices

        coin history

        coin history -a ETH -c BTC -o ETH-BTH-history.json

        coin listen collect run

        coin listen -a BTC,ETH,XMR,ZEC collect -t Trade run -t 30
    '''
    logging.basicConfig(level=getattr(logging, log_level.upper()))
    from .datafeeds import Datafeed
    state['datafeed'] = \
        Datafeed.factory(feed_name=feed, cache_dir=cache_dir,
                         requester=requester)
    state['output_stream'] = Stream()
    state['subscriptions'] = {}

@coin.command(name='list')
@click.option('--output', '-o', type=click.File('wt'), default='-')
@pass_state
def list_all(state, output):
    "List all available assets"
    datafeed = state['datafeed']
    assets_list = datafeed.get_list()
    write(assets_list, output, sep=' ')

@coin.command()
@click.option('--assets', '-a', multiple=True, default=DEFAULT_ASSETS,
              envvar=f'{ENVVAR_PREFIX}_ASSETS')
@click.option('--output', '-o', type=click.File('wt'), default='-')
@pass_state
def info(state, assets, output):
    "Info about the requested assets"
    assets = ','.join(assets).split(',')
    datafeed = state['datafeed']
    assets_info = datafeed.get_info(assets)
    write(assets_info, output)


@coin.command()
@click.option('--assets', '-a', multiple=True, default=DEFAULT_ASSETS,
              envvar=f'{ENVVAR_PREFIX}_ASSETS')
@click.option('--currencies', '-c', multiple=True, default=DEFAULT_CURRENCIES,
              envvar=f'{ENVVAR_PREFIX}_CURRENCIES')
@click.option('--output', '-o', type=click.File('wt'), default='-')
@pass_state
def prices(state, assets, currencies, output):
    'Latest asset prices'
    # FIXME: This should also use split here to be consistent
    assets = ','.join(assets)
    currencies = ','.join(currencies)
    datafeed = state['datafeed']
    prices = datafeed.get_prices(assets=assets, currencies=currencies)
    write(prices, output)


@coin.command()
@click.option('--freq', '-f', default='d', type=click.Choice(list('dhms')))
@click.option('--start-date', '-s', default=-30)
@click.option('--end-date', '-e', default=None)
@click.option('--assets', '-a', multiple=True, default=DEFAULT_ASSETS,
              envvar=f'{ENVVAR_PREFIX}_ASSETS')
@click.option('--currencies', '-c', multiple=True, default=DEFAULT_CURRENCIES,
              envvar=f'{ENVVAR_PREFIX}_CURRENCIES')
@click.option('--output', '-o', type=click.File('wt'), default='-')
@pass_state
def history(state, assets, currencies, freq, start_date, end_date, output):
    'Historic asset prices and volumes'
    assets = ','.join(assets).split(',')
    currencies = ','.join(currencies).split(',')
    datafeed = state['datafeed']
    data = []
    for asset, currency in product(assets, currencies):
        pair_data = datafeed.get_historical_data(
            asset, currency, freq=freq, start_date=start_date, end_date=end_date)
        data.extend(pair_data)
    write(data, output)


def tabulate(data):
    if isinstance(data, dict):
        data_iter = data.values()
    elif isinstance(data, list):
        data_iter = data
    else:
        raise TypeError(f'data: {data}')
    headers = set(chain(*map(lambda v: v.keys(), data_iter)))
    DataTuple = namedtuple('DataTuple', ' '.join(headers))
    DataTuple.__new__.__defaults__ = (None,) * len(headers)
    return map(lambda d: DataTuple(**d), data_iter)


@coin.command()
@click.option('--exchange', '-e', default='bitfinex',
              type=click.Choice(['bitfinex', 'luno']))
@click.option('--assets', '-a', multiple=True, default=DEFAULT_ASSETS,
              envvar=f'{ENVVAR_PREFIX}_ASSETS')
@click.option('--currencies', '-c', multiple=True, default=DEFAULT_CURRENCIES,
              envvar=f'{ENVVAR_PREFIX}_CURRENCIES')
@click.option('--channel', '-C', default='trades', type=click.Choice(['trades', 'ticker']))
@click.option('--api-key-id', default=None)
@click.option('--api-key-secret', default=None)
@pass_state
def listen(state, exchange, assets, currencies, channel, api_key_id,
           api_key_secret):
    'Listen to live events from an exchange'
    # FIXME: Use a factory function here
    from .exchanges import BitfinexExchange, LunoExchange
    assets = ','.join(assets).split(',')
    currencies = ','.join(currencies).split(',')
    pairs = list(map(''.join, product(assets, currencies)))
    output_stream = state['output_stream']
    subscriptions = state['subscriptions']
    if exchange=='bitfinex':
        exchange = BitfinexExchange(output_stream=output_stream)
        for pair in pairs:
            subscription = exchange.listen(pair, channel)
            subscriptions[f'{pair}-{exchange}'] = subscription
    elif exchange=='luno':
        if api_key_id is None:
            api_key_id = (config['Luno'].get('api_key_id', '') if 'Luno' in
                          config else '')
            api_key_secret = (config['Luno'].get('api_key_secret', '') if
                              'Luno' in config else '')
        exchange = LunoExchange(output_stream=output_stream,
                                api_key_id=api_key_id,
                                api_key_secret=api_key_secret)
        for pair in pairs:
            subscription = exchange.listen(pair)
            subscriptions[f'{pair}-{exchange}'] = subscription
    else:
       raise NotImplementedError()
 

@coin.command()
@click.option('--filter', '-f', default='', type=str, multiple=True)
@click.option('--type', '-t', default=None,
              type=click.Choice(['None', 'Trade', 'Heartbeat']))
@click.option('--output', '-o', default='-', type=click.File('w'))
@click.option('--events', 'format', flag_value='events', default=True)
@click.option('--json', 'format', flag_value='json')
@pass_state
def collect(state, filter, type, output, format):
    'Collect events and write them to an output sink'
    # TODO: The filter logic here should be moved to the collectors module
    output_stream = state['output_stream']
    if type:
        output_stream = output_stream.filter(
            lambda ev: ev.__class__.__name__==type)
    filters = filter
    for filter in filters:
        output_stream = output_stream.filter(
            lambda x: eval(filter, attr.asdict(x)))
    if format=='json':
        output_stream = output_stream.map(attr.asdict)
    sink = (output_stream
            .map(lambda ev: output.write(str(ev)+'\n'))
            .map(lambda ev: output.flush())
            )


@coin.command()
@click.option('--timeout', '-t', default=15)
@pass_state
def run(state, timeout):
    'Run the asyncio event loop for a set amount of time'
    if not timeout:
        # Allow to run indefinitely if timeout==0
        timeout = None
    subscriptions = state['subscriptions']
    loop = asyncio.get_event_loop()
    logger.debug('starting ...')
    completed, pending = \
        loop.run_until_complete(asyncio.wait(subscriptions.values(),
                                timeout=timeout))
    logger.debug('cancelling ...')
    for task in pending:
        task.cancel()
    logger.debug('finishing...')
    loop.run_until_complete(asyncio.sleep(1))
    logger.debug('done')


def write(data, file, sep='\n'):
    for record in data:
        file.write(str(record)+sep)


if __name__ == '__main__':
    # TODO: Check the click docs if the auto_envvars_prefix can be set
    #       elsewhere.
    coin(auto_envvars_prefix=f'{ENVVAR_PREFIX}')
