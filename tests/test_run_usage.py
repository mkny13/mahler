"""Accounting fixtures from the CLI event shapes documented in issue #416."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mahler import config, platforms, run_usage
from mahler.cli import cmd_backfill_usage
from mahler.ledger import Ledger


class UsageTests(unittest.TestCase):
    def log(self, kind, events, model=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'agent.log'
            path.write_text('\n'.join(json.dumps(e) for e in events))
            return platforms.read_log(path, kind, model)

    def test_claude(self):
        def quota(a, b):
            return {'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': {
                'five_hour': {'utilization': a}, 'seven_day': {'utilization': b}}}}
        log = self.log('claude', [
            {'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5'},
            quota(.1, .2), quota(.112, .204),
            {'type': 'result', 'subtype': 'success', 'total_cost_usd': .12, 'usage': {
                'input_tokens': 100, 'cache_creation_input_tokens': 20,
                'cache_read_input_tokens': 40, 'output_tokens': 15,
                'output_tokens_details': {'thinking_tokens': 5}}}])
        self.assertEqual(log['tokens'], {'in': 120, 'cached': 40, 'out': 15, 'reasoning': 5})
        self.assertEqual(log['quota_used'], {'5h': 1.2, 'weekly': .4})
        self.assertEqual(log['model'], 'claude-opus-5-5')
        self.assertEqual(log['cost_usd'], .12)

    def test_codex_sums_turns_and_subtracts_cache(self):
        log = self.log('codex', [{'type': 'turn.completed', 'usage': {
            'input_tokens': 100, 'cached_input_tokens': 60,
            'output_tokens': 20, 'reasoning_output_tokens': 5}}] * 2, 'configured')
        self.assertEqual(log['tokens'], {'in': 80, 'cached': 120, 'out': 40, 'reasoning': 10})
        self.assertEqual(log['model'], 'configured')

    def test_agy(self):
        log = self.log('agy', [{'event': 'init', 'model': 'gemini'},
            {'event': 'result', 'result': {'usage': {'input_tokens': 1,
             'output_tokens': 2, 'thinking_tokens': 3, 'cache_read_tokens': 4}}}])
        self.assertEqual(log['tokens'], {'in': 1, 'cached': 4, 'out': 2, 'reasoning': 3})
        self.assertEqual(log['model'], 'gemini')

    def test_cline(self):
        log = self.log('cline', [{'type': 'run_result', 'model': 'cline-model',
            'aggregateUsage': {'inputTokens': 10, 'outputTokens': 2,
                               'cacheReadTokens': 3, 'cacheWriteTokens': 4}}])
        self.assertEqual(log['tokens'], {'in': 14, 'cached': 3, 'out': 2, 'reasoning': 0})
        self.assertEqual(log['model'], 'cline-model')

    def test_copilot_last_credit_checkpoint_without_invented_tokens(self):
        log = self.log('copilot', [
            {'type': 'assistant.message', 'data': {'model': 'copilot-model'}},
            {'type': 'session.usage_checkpoint', 'data': {'totalNanoAiu': 1e9}},
            {'type': 'session.usage_checkpoint', 'data': {'totalNanoAiu': 2.5e9}}])
        self.assertEqual(log['credits'], 2.5)
        self.assertEqual(log['model'], 'copilot-model')
        self.assertTrue(all(v is None for v in log['tokens'].values()))

    def test_kilo_sums_steps(self):
        log = self.log('kilo', [{'type': 'step_finish', 'part': {
            'model': {'modelID': 'kilo-model'}, 'tokens': {
                'input': 10, 'output': 2, 'reasoning': 3, 'cache': {'read': 4}}}}] * 2)
        self.assertEqual(log['tokens'], {'in': 20, 'cached': 8, 'out': 4, 'reasoning': 6})
        self.assertEqual(log['model'], 'kilo-model')

    def test_unknown_and_malformed_lines(self):
        log = self.log('codex', [None, [], {'type': 'turn.completed', 'usage': {}}])
        self.assertIsNone(log['tokens']['out'])
        self.assertIsNone(log['cost_usd'])

    def test_pricing(self):
        cfg = {'platforms': {'p': {'build_model': 'm', 'sort_model': 's'}},
               'prices': {'m': {'in': 2, 'out': 10}}}
        run = {'platform': 'p', 'role': 'build', 'model': None}
        log = {'tokens': {'in': 100, 'cached': 50, 'out': 20, 'reasoning': 10}}
        cols = run_usage.columns(log, run, cfg)
        self.assertAlmostEqual(cols['cost_usd'], .0006)
        self.assertEqual(cols['cost_source'], 'priced')
        cfg['prices']['m']['cached_in'] = .2
        self.assertAlmostEqual(run_usage.columns(log, run, cfg)['cost_usd'], .00051)
        log['cost_usd'] = 0
        self.assertEqual(run_usage.columns(log, run, cfg)['cost_source'], 'cli')
        del log['cost_usd']
        run['model'] = 'historical'
        self.assertIsNone(run_usage.columns(log, run, cfg)['cost_usd'])
        self.assertEqual(run_usage.columns(log, run, cfg)['cost_source'], 'unpriced')
        self.assertEqual(run_usage.columns(log, run, cfg)['model'], 'historical')
        log['model'] = 'actual'
        self.assertEqual(run_usage.columns(log, run, cfg)['model'], 'actual')
        self.assertIsNone(run_usage.columns({}, run, cfg)['tokens_out'])

    def test_config_prices_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.toml'
            p.write_text('[prices."custom"]\nin=3\nout=12\n')
            cfg = config.load(p)
        self.assertEqual(cfg['prices']['custom']['in'], 3)
        self.assertEqual(cfg['prices']['gpt-6-luna']['out'], .5)

    def test_backfill_dry_run_idempotence_and_skips(self):
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(str(Path(d) / 'ledger.db'))
            self.addCleanup(led.close)
            path = Path(d) / 'agent.log'
            path.write_text(json.dumps({'type': 'turn.completed', 'usage': {
                'input_tokens': 100, 'output_tokens': 10}}))
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg['platforms']['p'] = {'kind': 'codex', 'build_model': 'gpt-6-luna'}
            def create(**kw):
                return led.create_run(project='p', number=1, role='build', platform='p',
                                      epoch=0, log_path=str(path), **kw)
            good = create(status='ended')
            active = create()
            failed = create(status='ended', outcome='launch failed: missing executable')
            missing = create(status='ended')
            led.update_run(missing, log_path=str(Path(d) / 'absent'))
            credit = create(status='ended')
            credit_path = Path(d) / 'credit.log'
            credit_path.write_text(json.dumps({'type': 'session.usage_checkpoint',
                                              'data': {'totalNanoAiu': 1e9}}))
            cfg['platforms']['c'] = {'kind': 'copilot'}
            led.update_run(credit, platform='c', log_path=str(credit_path))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cmd_backfill_usage(SimpleNamespace(dry_run=True), cfg, led)
                self.assertIsNone(led.run(good)['tokens_out'])
                cmd_backfill_usage(SimpleNamespace(dry_run=False), cfg, led)
                cmd_backfill_usage(SimpleNamespace(dry_run=False), cfg, led)
            self.assertIn('Would update 2 run(s); skipped 2', output.getvalue())
            self.assertIn('Updated 0 run(s); skipped 2', output.getvalue())
            self.assertEqual(led.run(good)['tokens_out'], 10)
            self.assertEqual(led.run(good)['model'], 'gpt-6-luna')
            self.assertEqual(led.run(credit)['credits'], 1)
            self.assertIsNone(led.run(credit)['tokens_out'])
            for rid in (active, failed, missing):
                self.assertIsNone(led.run(rid)['cost_source'])

    def test_backfill_reprices_unpriced_runs_from_stored_tokens(self):
        led = Ledger(':memory:')
        self.addCleanup(led.close)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['prices'] = {'new-model': {'in': 2, 'cached_in': 1, 'out': 10}}
        tokens = dict(tokens_in=1000, tokens_cached=500, tokens_out=100, tokens_reasoning=50)
        def create(model, status='ended', **cols):
            rid = led.create_run(project='p', number=1, role='build', platform='gone',
                                 epoch=0, log_path='/nonexistent/agent.log', status=status)
            led.update_run(rid, model=model, **cols)
            return rid
        stale = create('new-model', cost_source='unpriced', **tokens)
        still = create('no-price-yet', cost_source='unpriced', **tokens)
        no_tokens = create('new-model', cost_source='unpriced', credits=3)
        cli = create('new-model', cost_source='cli', cost_usd=0.5, **tokens)
        running = create('new-model', status='running', cost_source='unpriced', **tokens)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cmd_backfill_usage(SimpleNamespace(dry_run=True), cfg, led)
            self.assertEqual(led.run(stale)['cost_source'], 'unpriced')
            cmd_backfill_usage(SimpleNamespace(dry_run=False), cfg, led)
            cmd_backfill_usage(SimpleNamespace(dry_run=False), cfg, led)
        self.assertIn('Would re-price 1 unpriced run(s)', output.getvalue())
        self.assertIn('Re-priced 1 unpriced run(s)', output.getvalue())
        self.assertIn('Re-priced 0 unpriced run(s)', output.getvalue())
        self.assertEqual(led.run(stale)['cost_source'], 'priced')
        # (1000*2 + 500*1 + (100+50)*10) / 1e6, the same formula columns() uses
        self.assertAlmostEqual(led.run(stale)['cost_usd'], 0.004)
        self.assertEqual(led.run(stale)['cost_usd'], run_usage.columns(
            {'tokens': {'in': 1000, 'cached': 500, 'out': 100, 'reasoning': 50}},
            {'model': 'new-model'}, cfg)['cost_usd'])
        for rid in (still, no_tokens, running):
            self.assertEqual(led.run(rid)['cost_source'], 'unpriced')
            self.assertIsNone(led.run(rid)['cost_usd'])
        self.assertEqual(led.run(cli)['cost_source'], 'cli')
        self.assertEqual(led.run(cli)['cost_usd'], 0.5)


FIXTURES = Path(__file__).parent / 'fixtures'


class RequestEvidenceTests(unittest.TestCase):
    """mahler#735. Cline signal: `iteration_start`, from @cline/shared
    agents/types.d.ts (cline 3.0.68 / @cline/core 0.0.90) — one model call per
    iteration, wrapped in agent_event."""

    def read(self, kind, lines, name='agent.log'):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / name
            path.write_text('\n'.join(l if isinstance(l, str) else json.dumps(l) for l in lines))
            return platforms.read_log(path, kind)

    def kilo(self, ts, pid, model=None):
        start = {'type': 'step_start', 'timestamp': ts, 'part': {'id': pid}}
        end = {'type': 'step_finish', 'part': {'model': {'modelID': model}} if model else {}}
        return [start, end]

    def test_kilo_fixture_counts_steps_not_tool_or_text_events(self):
        log = platforms.read_log(FIXTURES / 'kilo_requests.log', 'kilo')
        self.assertEqual(log['requests'], 2)
        self.assertEqual(log['request_coverage'], 'complete')
        # model switch and midnight crossing; "429"/"rate limit" in assistant text is not a limit
        self.assertEqual(log['request_buckets'], {'2026-09-07': {'kilo-a:free': 1},
                                                  '2026-09-08': {'kilo-b:free': 1}})
        self.assertIs(log['rate_limited'], False)
        self.assertFalse(log['quota_hit'])

    def test_cline_fixture_counts_iterations_and_dedupes(self):
        log = platforms.read_log(FIXTURES / 'cline.log', 'cline')
        self.assertEqual(log['requests'], 2)          # duplicate iteration 2 counted once
        self.assertEqual(log['request_coverage'], 'complete')
        self.assertEqual(log['request_buckets'], {'unknown': {'unknown': 2}})
        self.assertEqual(log['tokens']['out'], 300)

    def test_cline_without_request_event_is_unknown_not_zero(self):
        log = self.read('cline', [{'type': 'agent_event', 'event': {'type': 'tool_call'}},
                                  {'type': 'run_result', 'finishReason': 'completed'}])
        self.assertIsNone(log['requests'])
        self.assertIsNone(log['request_coverage'])
        self.assertIsNone(log['request_buckets'])

    def test_cline_partial_without_run_result(self):
        log = self.read('cline', [{'type': 'agent_event', 'event': {
            'type': 'iteration_start', 'iteration': 1, 'timestamp': '2026-09-07T23:59:00Z'}}])
        self.assertEqual((log['requests'], log['request_coverage']), (1, 'partial'))
        self.assertEqual(log['request_buckets'], {'2026-09-07': {'unknown': 1}})

    def test_absent_timestamps_keep_total_with_unknown_day(self):
        lines = self.kilo(None, 'p1', 'm') + self.kilo(1788825590000, 'p2', 'm')
        log = self.read('kilo', lines)
        self.assertEqual(log['requests'], 2)
        self.assertEqual(log['request_buckets'], {'unknown': {'m': 1}, '2026-09-07': {'m': 1}})

    def test_unknown_model_stays_unknown(self):
        log = self.read('kilo', self.kilo(1788825590000, 'p1'))
        self.assertEqual(log['request_buckets'], {'2026-09-07': {'unknown': 1}})

    def test_resume_segments_and_duplicate_ids_count_once(self):
        first = self.kilo(1788825590000, 'p1', 'm')
        resumed = first + self.kilo(1788825700000, 'p3', 'm')      # appended segment replays p1
        self.assertEqual(self.read('kilo', resumed)['requests'], 2)

    def test_known_zero_vs_unknown_vs_partial(self):
        zero = self.read('kilo', [{'type': 'error', 'error': {'data': {'statusCode': 401}}}])
        self.assertEqual((zero['requests'], zero['request_coverage']), (0, 'complete'))
        self.assertIsNone(self.read('kilo', [])['requests'])               # empty log
        self.assertIsNone(platforms.read_log('/nonexistent/agent.log', 'kilo')['requests'])
        open_step = self.read('kilo', [{'type': 'step_start', 'part': {'id': 'p'}}])
        self.assertEqual((open_step['requests'], open_step['request_coverage']), (1, 'partial'))
        truncated = self.read('kilo', self.kilo(1, 'p1', 'm') + ['{"type":"step_sta'])
        self.assertEqual((truncated['requests'], truncated['request_coverage']), (1, 'partial'))

    def test_other_platforms_have_no_request_accounting(self):
        log = self.read('codex', [{'type': 'turn.completed', 'usage': {}}])
        self.assertIsNone(log['requests'])
        self.assertIsNone(log['rate_limited'])

    def test_error_path_429(self):
        log = self.read('kilo', self.kilo(1, 'p1', 'm') + [
            {'type': 'error', 'error': {'data': {'statusCode': 429, 'message': 'slow down'}}}])
        self.assertIs(log['rate_limited'], True)
        cols = run_usage.columns(log, {'platform': 'kilo'}, {})
        self.assertEqual((cols['rate_limited'], cols['limit_hit'], cols['requests']), (1, 1, 1))
        log = self.read('cline', [{'type': 'error', 'error': 'Rate limit exceeded'}])
        self.assertIs(log['rate_limited'], True)

    def test_columns_distinguish_zero_unknown_and_replace_idempotently(self):
        log = platforms.read_log(FIXTURES / 'kilo_requests.log', 'kilo')
        first = run_usage.columns(log, {'platform': 'kilo'}, {})
        again = run_usage.columns(platforms.read_log(FIXTURES / 'kilo_requests.log', 'kilo'),
                                  {'platform': 'kilo'}, {})
        self.assertEqual(first, again)
        self.assertEqual((first['requests'], first['rate_limited'], first['limit_hit']), (2, 0, 0))
        unknown = run_usage.columns(platforms.read_log('/nonexistent', 'kilo'), {'platform': 'kilo'}, {})
        self.assertIsNone(unknown['requests'])
        self.assertIsNone(unknown['limit_hit'])
