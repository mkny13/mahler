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
