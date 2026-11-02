import asyncio
from datetime import datetime, timedelta
import io
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'mtproto'))
import traffic_stats
import traffic_accounting as relay


class TrafficStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'traffic.db';self.store=traffic_stats.TrafficStore(self.path)
        self.day=datetime.now(traffic_stats.ZONE).date().isoformat()

    def report(self, epoch='a', up=100, down=200, client='192.168.1.8', day=None, service='proxy'):
        return {'epoch':epoch,'clients':[dict(day=day or self.day,client=client,service=service,upload=up,download=down)]}

    def test_repeated_snapshots_and_restarts_do_not_double_count(self):
        self.store.ingest('core',self.report())
        self.store.ingest('core',self.report())
        self.store=traffic_stats.TrafficStore(self.path)
        self.store.ingest('core',self.report(up=150,down=300))
        self.store.ingest('core',self.report(epoch='b',up=20,down=40))
        result=self.store.summary('year')
        self.assertEqual(result['totals'],{'upload':170,'download':340})
        self.assertEqual(sum(x['upload'] for x in result['clients']),170)

    def test_day_month_year_boundaries_and_counter_rollback(self):
        for day in ['2025-12-31','2026-01-01']:
            self.store.ingest('core',self.report(day=day))
        self.store.ingest('core',self.report(day='2026-01-01',up=10,down=20))
        self.store.ingest('core',self.report(day='2026-01-01',up=15,down=30))
        self.assertEqual(self.store.summary('month','2025')['totals']['upload'],100)
        self.assertEqual(self.store.summary('day','2026-01')['totals']['download'],210)
        self.assertEqual(self.store.summary('year')['totals']['upload'],205)

    def test_internal_relay_is_excluded_and_original_client_is_counted(self):
        self.store.ingest('core',self.report(client='172.18.0.3'),excluded={'172.18.0.3'})
        self.store.ingest('relay',self.report(service='whatsapp'))
        result=self.store.summary('year')
        self.assertEqual(result['totals'],{'upload':100,'download':200})
        self.assertEqual(result['clients'][0]['id'],'192.168.1.8')

    def test_idle_source_is_not_recharged_when_other_source_is_active(self):
        old=(datetime.now(traffic_stats.ZONE).date()-timedelta(days=90)).isoformat()
        self.store.ingest('relay',self.report(day=old,service='telegram'))
        for _ in range(3):
            self.store.ingest('core',self.report())
            self.store.ingest('relay',self.report(day=old,service='telegram'))
        self.assertEqual(self.store.summary('year')['totals']['upload'],200)

    def test_alias_filter_and_online_backup(self):
        self.store.ingest('core',self.report())
        self.store.alias('192.168.1.8','客厅 Kindle')
        result=self.store.summary('year',client='192.168.1.8')
        self.assertEqual(result['clients'][0]['label'],'客厅 Kindle')
        target=Path(self.temp.name)/'backup.db';self.store.backup(target)
        self.assertEqual(traffic_stats.TrafficStore(target).summary('year')['totals'],result['totals'])

    def test_invalid_counters_and_names_cannot_pollute_history(self):
        report=self.report();report['clients'][0]['upload']=-1
        self.store.ingest('core',report)
        self.store.ingest('core',self.report(client='../../private'))
        self.assertEqual(self.store.summary('year')['totals']['upload'],0)
        with self.assertRaises(ValueError):self.store.summary('bad')


class RelayAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relay._rows.clear();relay._counts.clear();relay._last_day=''

    async def test_closed_short_flow_and_partial_read_are_retained(self):
        reader=asyncio.StreamReader();reader.feed_data(b'abc');reader.feed_eof()
        wrapped=relay.CountingReader(reader,'telegram','192.168.1.9')
        with self.assertRaises(asyncio.IncompleteReadError):await wrapped.readexactly(5)
        report=relay.snapshot()
        self.assertEqual(report['clients'][0]['upload'],3)
        self.assertEqual((await wrapped.read()),b'')
        self.assertEqual(relay.snapshot()['clients'][0]['upload'],3)

    async def test_relay_service_and_midnight_are_separate(self):
        t=datetime(2025,12,31,23,59,59,tzinfo=relay.ZONE).timestamp()
        relay.record('telegram','::ffff:192.168.1.9',upload=5,now=t)
        relay.record('whatsapp','192.168.1.9',download=7,now=t+1)
        rows=relay.snapshot()['clients']
        self.assertEqual({x['day'] for x in rows},{'2025-12-31','2026-01-01'})
        self.assertEqual({x['client'] for x in rows},{'192.168.1.9'})


if __name__=='__main__':unittest.main()
