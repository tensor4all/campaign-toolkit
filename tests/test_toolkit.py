"""Self-test for the toolkit, against a synthetic fixture campaign in tests/fixture.

It never builds or measures anything: it exercises the content paths only --
descriptor validation, manifest validation, the report, publication and the
generated index, and the run comparison. A throwaway git repository stands in for
the measured checkout so the recorded commit resolves the way it does in a real
campaign.
"""
import contextlib
import hashlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from unittest import mock

TOOLKIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLKIT))

from campaign_toolkit import (  # noqa: E402
    campaign, compare_runs, descriptor, gen_index, jsonschema_lite, publish_report,
    record_run, report_run, union_index, validate_run,
)

FIXTURE = TOOLKIT / "tests/fixture"
SUITE_REL = "benchmarks/suites/fixture-kernels.yaml"
RUN_A_REL = "data/results/fixture-cpu/fixture-kernels/20260101T000000Z"
RUN_B_REL = "data/results/fixture-cpu/fixture-kernels/20260102T000000Z"

# The toolkit must hold no campaign's specifics: everything comes from the
# descriptor. A campaign's name here would mean something leaked.
FORBIDDEN_IN_PACKAGE = ("tlinalg", "faer", "tlbench", "fbench")


def git(cwd, *args, **env):
    full = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid", **env)
    return subprocess.run(["git", *args], cwd=cwd, env=full, check=True,
                          capture_output=True, text=True).stdout.strip()


class FixtureCampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "campaign"
        shutil.copytree(FIXTURE, self.root)

        # A checkout the recorded commit can resolve in, standing in for the
        # measured project.
        self.checkout = self.root / "extern/fixture-rs"
        self.checkout.mkdir(parents=True)
        git(self.checkout, "init", "-q")
        (self.checkout / "README").write_text("fixture\n")
        git(self.checkout, "add", "README")
        git(self.checkout, "commit", "-q", "-m", "fixture")
        rev = git(self.checkout, "rev-parse", "HEAD")

        suite = self.root / SUITE_REL
        sha = hashlib.sha256(suite.read_bytes()).hexdigest()
        for path in self.root.rglob("*"):
            if path.suffix in (".yaml", ".rev"):
                text = path.read_text().replace("@COMMIT@", rev).replace("@SUITE_SHA@", sha)
                path.write_text(text)
        descriptor.set_root(self.root)
        # The fixture runs the recorder in-process, so the real test runner is one of its
        # ancestors with the whole machine in its affinity. The ancestor check is exercised by its
        # own test; here it is held at "nothing to see" so the fixture topology decides the flow.
        patcher = mock.patch.object(record_run, 'ancestor_processes', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_descriptor_loads(self):
        d = descriptor.load_descriptor(self.root)
        self.assertEqual(d["id"], "fixture-campaign")
        self.assertEqual(d.identities, ["library"])
        self.assertEqual(d.engines["algo-lane"], "algo")
        self.assertEqual(d.vendor_providers, ("openblas",))
        self.assertEqual(d.measured_providers, ("algo",))
        self.assertEqual(len(d.csv_columns), 12)

    def test_v2_toolkit_identity_rejects_distributions_and_enclosing_repos(self):
        identity = record_run.toolkit_identity(self.checkout)
        self.assertEqual(identity['commit'], git(self.checkout, 'rev-parse', 'HEAD'))
        self.assertFalse(identity['dirty'])
        nested = self.checkout / '.venv/lib/site-packages'
        nested.mkdir(parents=True)
        # The old plain rev-parse silently reported the enclosing project's Git.
        self.assertEqual(git(nested, 'rev-parse', '--show-toplevel'), str(self.checkout))
        outside = pathlib.Path(self.tmp.name) / 'site-packages'
        outside.mkdir()
        for root in (nested, outside):
            with self.assertRaisesRegex(SystemExit, 'editable.*Git checkout'):
                record_run.toolkit_identity(root)
        (self.checkout / 'untracked-source').write_text('dirty\n')
        self.assertTrue(record_run.toolkit_identity(self.checkout)['dirty'])

    def _commit_campaign(self):
        (self.root / ".gitignore").write_text("extern/\ntarget/\n")
        (self.root / "Cargo.lock").write_text("# lock\n")
        (self.root / "result").mkdir(exist_ok=True)
        (self.root / "result/INDEX.md").write_text("index\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "campaign")

    def test_recorders_own_outputs_are_not_harness_source(self):
        """A recipe's second recording must not be refused for the first one's published pages."""
        self._commit_campaign()
        d = descriptor.load_descriptor(self.root)
        clean = record_run.harness_source_state(d)
        self.assertEqual(clean, {'dirty': False, 'outputs_dirty': False,
                                 'excluded_outputs': ['data/results', 'result']})
        # The previous recording: a modified index, a new page, a new run directory.
        (self.root / "result/INDEX.md").write_text("index, now with the first arm\n")
        (self.root / "result/fixture-cpu").mkdir()
        (self.root / "result/fixture-cpu/page.md").write_text("page\n")
        run = self.root / "data/results/fixture-cpu/fixture-kernels/20270101T000000Z"
        run.mkdir(parents=True)
        (run / "run.yaml").write_text("x: 1\n")
        state = record_run.harness_source_state(d)
        self.assertFalse(state['dirty'])
        self.assertTrue(state['outputs_dirty'])
        # A modified tracked output under data/ (not just new files) is an output too.
        git(self.root, "add", "-f", "data/results")
        git(self.root, "commit", "-q", "-m", "run")
        (run / "run.yaml").write_text("x: 2\n")
        self.assertFalse(record_run.harness_source_state(d)['dirty'])

    def test_harness_source_changes_are_still_dirty(self):
        self._commit_campaign()
        d = descriptor.load_descriptor(self.root)
        for rel in ("Cargo.lock", SUITE_REL):
            path = self.root / rel
            original = path.read_text()
            path.write_text(original + "# edited\n")
            self.assertTrue(record_run.harness_source_state(d)['dirty'], rel)
            path.write_text(original)
            self.assertFalse(record_run.harness_source_state(d)['dirty'], rel)
        # Edited source together with a dirty output is still dirty.
        (self.root / "result/INDEX.md").write_text("changed\n")
        (self.root / "Cargo.lock").write_text("# changed\n")
        state = record_run.harness_source_state(d)
        self.assertTrue(state['dirty'] and state['outputs_dirty'])

    def test_moved_check_ignores_outputs_but_sees_source_edits(self):
        """Between process sets and at the end: outputs are not 'moved', a source edit is."""
        self._commit_campaign()
        d = descriptor.load_descriptor(self.root)
        head = git(self.root, "rev-parse", "HEAD")
        self.assertFalse(record_run.source_changed(self.root, head, d))
        # Outputs modified before the recording (the previous arm) and during it.
        (self.root / "result/INDEX.md").write_text("previous arm\n")
        self.assertFalse(record_run.source_changed(self.root, head, d))
        (self.root / "result/INDEX.md").write_text("during the recording\n")
        self.assertFalse(record_run.source_changed(self.root, head, d))
        # A source edit mid-run is moved.
        (self.root / "Cargo.lock").write_text("# edited mid-run\n")
        self.assertTrue(record_run.source_changed(self.root, head, d))
        git(self.root, "checkout", "Cargo.lock")
        self.assertFalse(record_run.source_changed(self.root, head, d))
        # Other checkouts are still judged over the whole tree, outputs-named paths included.
        (self.checkout / "README").write_text("edited\n")
        self.assertTrue(record_run.source_changed(
            self.checkout, git(self.checkout, "rev-parse", "HEAD"), d))

    def test_record_propagates_index_failure_and_honors_no_publish(self):
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        source_csv = self.root / RUN_A_REL / original['result_files'][0]
        pin = {'FIXTURE_REV': original['library']['commit'], 'FIXTURE_DIRTY': 'false',
               'FIXTURE_DIR': str(self.checkout), 'BUILD_FEATURES': '', 'BIN_DIR': str(self.root)}
        for no_publish in (False, True):
            modules = []

            def child(module, args, **kwargs):
                modules.append(module)
                code = (37 if module == 'gen_index' else
                        publish_report.main(args, root=self.root) if module == 'publish_report' else 0)
                return subprocess.CompletedProcess(args, code, stdout='', stderr='')

            def process(args, **kwargs):
                if '--csv' in args:
                    csv_path = args[args.index('--csv') + 1]
                    self.assertEqual(kwargs['env']['PINNED_CSV'], csv_path)
                    shutil.copyfile(source_csv, csv_path)
                elif 'campaign_toolkit.report_run' in args:
                    directory = pathlib.Path(args[args.index('--run-dir') + 1])
                    (directory / 'report.md').write_text('Synthetic failure-propagation test report.\n')
                return subprocess.CompletedProcess(args, 0, stdout='all comparisons within tolerance', stderr='')

            with (mock.patch.object(record_run, 'build', return_value=pin),
                  # The fixture asserts the pipeline, not the host's power state: a CI runner with
                  # turbo enabled must not decide whether this test can run.
                  mock.patch.object(record_run, 'boost_state', return_value={}),
                  mock.patch.object(record_run.observer_audit, 'offenders', return_value=([], [], 0)),
                  mock.patch.object(record_run.os, 'sched_getaffinity', return_value={8}),
                  mock.patch.object(record_run, 'git', side_effect=lambda path, *args:
                                    original['library']['commit'] if args[0] == 'rev-parse' else ''),
                  mock.patch.object(record_run, 'protocol_script', return_value=descriptor.current().profiles_file),
                  mock.patch.object(record_run, 'vendor_identity', return_value={'vendor.name': 'openblas'}),
                  mock.patch.object(record_run, 'host_info', return_value=original['host']),
                  mock.patch.object(record_run.subprocess, 'run', side_effect=process),
                  mock.patch.object(descriptor, 'run_module', side_effect=child)):
                args = ['fixture-cpu', 'fixture-kernels', '--threads', '1', '--label', str(no_publish)]
                code = record_run.main(args + (['--no-publish'] if no_publish else []), root=self.root)
            self.assertEqual(code, 0 if no_publish else 37)
            self.assertEqual('gen_index' in modules, not no_publish)
            self.assertEqual('publish_report' in modules, not no_publish)

    def test_harness_args_reach_run_and_verify_but_not_info(self):
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        source_csv = self.root / RUN_A_REL / original['result_files'][0]
        pin = {'FIXTURE_REV': original['library']['commit'], 'FIXTURE_DIRTY': 'false',
               'FIXTURE_DIR': str(self.checkout), 'BUILD_FEATURES': '', 'BIN_DIR': str(self.root)}
        seen = []

        def process(args, **kwargs):
            seen.append(list(args))
            if '--csv' in args:
                shutil.copyfile(source_csv, args[args.index('--csv') + 1])
            elif 'campaign_toolkit.report_run' in args:
                directory = pathlib.Path(args[args.index('--run-dir') + 1])
                (directory / 'report.md').write_text('harness-args test report\n')
            return subprocess.CompletedProcess(args, 0, stdout='all comparisons within tolerance', stderr='')

        # The fixture campaign is synthetic and must record on whatever machine runs the tests,
        # so the audit of the *real* machine is stubbed out here; it has its own tests.
        with (mock.patch.object(record_run, 'build', return_value=pin),
              mock.patch.object(record_run, 'boost_state', return_value={}),
              mock.patch.object(record_run.observer_audit, 'offenders', return_value=([], [], 0)),
              mock.patch.object(record_run.os, 'sched_getaffinity', return_value={8}),
              mock.patch.object(record_run, 'git', side_effect=lambda path, *a:
                                original['library']['commit'] if a[0] == 'rev-parse' else ''),
              mock.patch.object(record_run, 'protocol_script', return_value=descriptor.current().profiles_file),
              mock.patch.object(record_run, 'vendor_identity', return_value={'vendor.name': 'openblas'}),
              mock.patch.object(record_run, 'host_info', return_value=original['host']),
              mock.patch.object(record_run.subprocess, 'run', side_effect=process),
              mock.patch.object(descriptor, 'run_module',
                                side_effect=lambda *a, **k: subprocess.CompletedProcess([], 0, stdout='', stderr=''))):
            code = record_run.main(['fixture-cpu', 'fixture-kernels', '--threads', '1', '--no-publish'],
                                   root=self.root)
        self.assertEqual(code, 0)
        harness = [a for a in seen if '--' in a and a[a.index('--') + 1].endswith('fixturebench')]
        self.assertTrue(harness, 'the recorder never invoked the measured binary')
        for argv in harness:
            self.assertIn(argv[argv.index('--') + 2], ('verify', 'run'))
            self.assertIn('--corpus', argv)
            self.assertIn('fixture-instances', argv)

    def _record(self, suite_id, label, source_csv, extra=(), identity=None, external_json=None):
        """Record one run through the recorder with the build and measurement side mocked.

        The content path is exercised for real: `validate_run` runs in-process, so a manifest the
        recorder writes is checked by the same code CI checks it with.
        """
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        pin = {'FIXTURE_REV': original['library']['commit'], 'FIXTURE_DIRTY': 'false',
               'FIXTURE_DIR': str(self.checkout), 'BUILD_FEATURES': '', 'BIN_DIR': str(self.root)}

        def process(args, **kwargs):
            if '--csv' in args:
                shutil.copyfile(source_csv, args[args.index('--csv') + 1])
            elif 'campaign_toolkit.report_run' in args:
                (pathlib.Path(args[args.index('--run-dir') + 1])
                 / 'report.md').write_text('placeholder\n')
            return subprocess.CompletedProcess(args, 0,
                                               stdout='all comparisons within tolerance', stderr='')

        def child(module, args, **kwargs):
            if module == 'validate_run':
                errors = validate_run.validate(args[0])
                return subprocess.CompletedProcess(args, 1 if errors else 0,
                                                   stdout='\n'.join(errors), stderr='')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        argv = ['fixture-cpu', suite_id, '--threads', '1', '--label', label, '--no-publish', *extra]
        if external_json is not None:
            argv += ['--external-provider-json', str(external_json)]
        # The fixture campaign is synthetic and must record on whatever machine runs the tests,
        # so the audit of the *real* machine is stubbed out here; it has its own tests.
        with (mock.patch.object(record_run, 'build', return_value=pin),
              mock.patch.object(record_run, 'boost_state', return_value={}),
              mock.patch.object(record_run.observer_audit, 'offenders', return_value=([], [], 0)),
              mock.patch.object(record_run.os, 'sched_getaffinity', return_value={8}),
              mock.patch.object(record_run, 'git', side_effect=lambda path, *a:
                                original['library']['commit'] if a[0] == 'rev-parse' else ''),
              mock.patch.object(record_run, 'protocol_script',
                                return_value=descriptor.current().profiles_file),
              mock.patch.object(record_run, 'vendor_identity', return_value=identity or {}),
              mock.patch.object(record_run, 'host_info', return_value=original['host']),
              mock.patch.object(record_run.subprocess, 'run', side_effect=process),
              mock.patch.object(descriptor, 'run_module', side_effect=child)):
            self.assertEqual(record_run.main(argv, root=self.root), 0)
        runs = sorted((self.root / 'data/results/fixture-cpu' / suite_id).glob(f'*-{label}'))
        self.assertEqual(len(runs), 1, f'expected one {label} run under {suite_id}')
        return runs[0]

    def _record_capturing_env(self, suite_id, label, source_csv, extra=(), identity=None,
                              external_json=None):
        """Record one run with the *real* `build()`, whose `sh` writes the pin file, and capture
        the environment each child is given: the build's and the measured process's."""
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        build_envs, run_envs = [], []

        def fake_sh(cmd, **kwargs):
            build_envs.append(kwargs['env'])
            out = pathlib.Path(next(a for a in cmd if str(a).endswith('.env')))
            # Stand in for the campaign's build script: write the variable file the recorder reads
            # back, carrying the features it was invoked with the way a real build script does.
            out.write_text(f"FIXTURE_REV={original['library']['commit']}\n"
                           "FIXTURE_DIRTY=false\n"
                           f"FIXTURE_DIR={self.checkout}\n"
                           f"BUILD_FEATURES={kwargs['env'].get('BENCH_FEATURES', '')}\n"
                           f"BIN_DIR={self.root}\n")
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        def process(args, **kwargs):
            if 'verify' in args or 'run' in args:
                run_envs.append(kwargs.get('env') or {})
            if '--csv' in args:
                shutil.copyfile(source_csv, args[args.index('--csv') + 1])
            elif 'campaign_toolkit.report_run' in args:
                (pathlib.Path(args[args.index('--run-dir') + 1])
                 / 'report.md').write_text('placeholder\n')
            return subprocess.CompletedProcess(args, 0,
                                               stdout='all comparisons within tolerance', stderr='')

        def child(module, args, **kwargs):
            if module == 'validate_run':
                errors = validate_run.validate(args[0])
                return subprocess.CompletedProcess(args, 1 if errors else 0,
                                                   stdout='\n'.join(errors), stderr='')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        argv = ['fixture-cpu', suite_id, '--threads', '1', '--label', label, '--no-publish', *extra]
        if external_json is not None:
            argv += ['--external-provider-json', str(external_json)]
        with (mock.patch.object(record_run, 'sh', side_effect=fake_sh),
              mock.patch.object(record_run, 'boost_state', return_value={}),
              mock.patch.object(record_run.observer_audit, 'offenders', return_value=([], [], 0)),
              mock.patch.object(record_run.os, 'sched_getaffinity', return_value={8}),
              mock.patch.object(record_run, 'git', side_effect=lambda path, *a:
                                original['library']['commit'] if a[0] == 'rev-parse' else ''),
              mock.patch.object(record_run, 'protocol_script',
                                return_value=descriptor.current().profiles_file),
              mock.patch.object(record_run, 'vendor_identity', return_value=identity or {}),
              mock.patch.object(record_run, 'host_info', return_value=original['host']),
              mock.patch.object(record_run.subprocess, 'run', side_effect=process),
              mock.patch.object(descriptor, 'run_module', side_effect=child)):
            self.assertEqual(record_run.main(argv, root=self.root), 0)
        runs = sorted((self.root / 'data/results/fixture-cpu' / suite_id).glob(f'*-{label}'))
        self.assertEqual(len(runs), 1, f'expected one {label} run under {suite_id}')
        return runs[0], build_envs, run_envs

    def _external_source_csv(self):
        source = self.root / 'external-source.csv'
        source.write_text(
            "regime,family,dtype,m,n,batch,row,threads,total_ms,per_item_us,status,note\n"
            "small,mul,f64,4,4,1,algo-lane,1,0.4000,1.0000,ok,\n"
            "small,mul,f64,4,4,1,ext-baseline,1,0.5000,1.0000,ok,\n")
        return source

    def test_vendorless_run_records_the_external_identity_and_omits_vendor(self):
        run_dir = self._record(
            'fixture-external', 'ext', self._external_source_csv(),
            external_json=self.root / 'pins/external-providers.json')
        manifest = campaign.load(run_dir / 'run.yaml')
        # A suite with no vendor engine records no vendor identity at all: absence, not null.
        self.assertNotIn('vendor', manifest)
        self.assertEqual(manifest['run_spec']['engines'], ['algo-lane', 'ext-baseline'])
        providers = {p['name']: p for p in manifest['providers']}
        self.assertEqual(providers['algo']['note'],
                         'the provider in the measured revision; see library above')
        self.assertEqual(providers['tblis']['commit'],
                         '3f2a1c0b9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f40')
        self.assertEqual(providers['tblis']['tag'], 'v1.4.0')
        self.assertEqual(providers['tblis']['sha256'],
                         '9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b')
        self.assertNotIn('the provider in the measured revision', providers['tblis']['note'])
        # The report prints the external revision from the manifest, independently of Cargo deps.
        self.assertEqual(report_run.main(['--run-dir', str(run_dir)], root=self.root), 0)
        report = (run_dir / 'report.md').read_text()
        self.assertIn('- external providers: `tblis` 1.4.0 @ `3f2a1c0b', report)
        self.assertIn('tag `v1.4.0`', report)
        self.assertIn('sha256 `9a8b7c6d5e4f3a2b', report)

    def test_external_provider_admission_refuses_an_absent_or_empty_commit(self):
        import json

        source = self._external_source_csv()
        # No collector output at all, a collector that omits the provider, and a provider entry
        # that states no revision are the same refusal: nothing is published from an arm whose
        # revision cannot be stated.
        with self.assertRaisesRegex(SystemExit, "external provider\\(s\\) 'tblis'"):
            self._record('fixture-external', 'nojson', source)
        empty = self.root / 'empty-providers.json'
        empty.write_text('[]')
        with self.assertRaisesRegex(SystemExit, 'revision cannot be stated'):
            self._record('fixture-external', 'empty', source, external_json=empty)
        blank = self.root / 'blank-providers.json'
        blank.write_text(json.dumps([{'name': 'tblis', 'version': None, 'commit': '',
                                      'path': None, 'note': None}]))
        with self.assertRaisesRegex(SystemExit, 'revision cannot be stated'):
            self._record('fixture-external', 'blank', source, external_json=blank)

    def test_vendor_backed_fixture_still_records_validates_and_reports(self):
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        source = self.root / RUN_A_REL / original['result_files'][0]
        run_dir = self._record('fixture-kernels', 'vendor', source,
                               extra=['--vendor', 'openblas'],
                               identity={'vendor.name': 'openblas', 'vendor.linkage': 'static',
                                         'vendor.version': '0.3.99'})
        manifest = campaign.load(run_dir / 'run.yaml')
        self.assertEqual(manifest['vendor']['name'], 'openblas')
        self.assertTrue(any(p['name'] == 'openblas' for p in manifest['providers']))
        self.assertTrue(all('tag' not in p for p in manifest['providers']))
        self.assertEqual(report_run.main(['--run-dir', str(run_dir)], root=self.root), 0)
        self.assertNotIn('- external providers:', (run_dir / 'report.md').read_text())

    def test_suite_features_and_loader_paths_reach_the_child_processes(self):
        # The suite's `features` are what the build is invoked with, as `BENCH_FEATURES`, and the
        # descriptor's loader directories go on the measured process's `LD_LIBRARY_PATH` ahead of
        # whatever the recorder inherited. The fixture's external suite declares both.
        source = self._external_source_csv()
        with mock.patch.dict(os.environ, {'FIXTURE_EXT_ROOT': '/opt/fixture-ext',
                                          'LD_LIBRARY_PATH': '/inherited/lib'}):
            run_dir, build_envs, run_envs = self._record_capturing_env(
                'fixture-external', 'features', source,
                external_json=self.root / 'pins/external-providers.json')
        self.assertEqual(build_envs[0]['BENCH_FEATURES'], 'fixture-bench/link-tblis')
        self.assertTrue(run_envs)
        for env in run_envs:
            self.assertEqual(env['LD_LIBRARY_PATH'],
                             '/opt/fixture-ext/lib:/opt/fixture-ext/lib64:/inherited/lib')
        # The build round-trips the features into the manifest's library block.
        self.assertEqual(campaign.load(run_dir / 'run.yaml')['library']['features'],
                         ['fixture-bench/link-tblis'])

    def test_vendor_arm_feature_is_added_to_the_build_features(self):
        # `measured.features` is the descriptor's per-arm feature: the recorder reads it, so a
        # vendor-selected build is invoked with the feature that arm needs. The fixture's
        # kernels suite declares none of its own, so the arm's entry is the whole list.
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        source = self.root / RUN_A_REL / original['result_files'][0]
        with mock.patch.dict(os.environ, {'FIXTURE_EXT_ROOT': '/opt/fixture-ext'}):
            _, build_envs, run_envs = self._record_capturing_env(
                'fixture-kernels', 'armfeature', source,
                extra=['--vendor', 'openblas'], identity={'vendor.name': 'openblas'})
        self.assertEqual(build_envs[0]['BENCH_FEATURES'], 'fixture-bench/link-openblas')
        self.assertEqual(run_envs[0]['LD_LIBRARY_PATH'].split(os.pathsep)[:2],
                         ['/opt/fixture-ext/lib', '/opt/fixture-ext/lib64'])

    def test_absent_loader_paths_leave_the_environment_alone(self):
        # A campaign that declares no loader directories behaves exactly as before: the recorder
        # does not touch the measured process's LD_LIBRARY_PATH.
        import yaml as _yaml
        path = self.root / 'benchmarks/campaign.yaml'
        data = _yaml.safe_load(path.read_text())
        data.pop('loader_paths')
        path.write_text(_yaml.safe_dump(data, sort_keys=False))
        original = campaign.load(self.root / RUN_A_REL / 'run.yaml')
        source = self.root / RUN_A_REL / original['result_files'][0]
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/inherited/lib'}):
            _, _, run_envs = self._record_capturing_env(
                'fixture-kernels', 'noloader', source,
                extra=['--vendor', 'openblas'], identity={'vendor.name': 'openblas'})
        self.assertEqual(run_envs[0]['LD_LIBRARY_PATH'], '/inherited/lib')

    def test_observer_affinity_must_miss_the_measured_l3_domain(self):
        # CPU 7 is not measured when the cell runs on CPU 4, but it shares the cache: the observer
        # must be refused there too.
        with (mock.patch.object(record_run, 'l3_domain', return_value={4, 5, 6, 7, 16, 17, 18, 19}),
              mock.patch.object(record_run, 'online_cpus', return_value={0, 1, 2, 3, 4, 5, 6, 7})):
            with mock.patch.object(record_run.os, 'sched_getaffinity', return_value={0, 1, 2, 3}):
                record_run.require_observer_off_measured_cpus({'1': '4'})       # off the domain
            with mock.patch.object(record_run.os, 'sched_getaffinity', return_value={7}):
                with self.assertRaisesRegex(SystemExit, 'inside the measured L3 domain'):
                    record_run.require_observer_off_measured_cpus({'1': '4'})

    def test_spanning_domains_warns_instead_of_refusing(self):
        with mock.patch.object(record_run, 'l3_domain', return_value={4, 5, 6, 7, 16, 17, 18, 19}):
            with mock.patch('sys.stderr', new_callable=io.StringIO) as quiet:
                record_run.warn_if_measured_cpus_span_l3_domains({'4': '4-7'})   # one domain
                self.assertEqual(quiet.getvalue(), '')
        with mock.patch.object(record_run, 'l3_domain',
                               side_effect=lambda cpu: {0, 1} if cpu < 4 else {4, 5}):
            with mock.patch('sys.stderr', new_callable=io.StringIO) as loud:
                record_run.warn_if_measured_cpus_span_l3_domains({'8': '0-7'})
                self.assertIn('spans the L3 domains', loud.getvalue())           # warned, not refused

    def test_a_worker_ancestor_on_a_measured_cpu_refuses_before_the_build(self):
        # Pinning the recorder does nothing for the agent that launched it: the agent is a separate
        # long-lived process and keeps using the cores while the cells are timed.
        topo = (mock.patch.object(record_run, 'l3_domain', return_value={4, 5, 6, 7, 16, 17, 18, 19}),
                mock.patch.object(record_run, 'online_cpus',
                                  return_value={0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15}),
                mock.patch.object(record_run.os, 'sched_getaffinity',
                                  return_value={0, 1, 2, 3, 12, 13, 14, 15}))
        with topo[0], topo[1], topo[2]:
            agent = [{'pid': 4242, 'comm': 'node', 'affinity': set(range(24)), 'cpu_fraction': 0.24}]
            with mock.patch.object(record_run, 'ancestor_processes', return_value=agent):
                with self.assertRaisesRegex(SystemExit, 'observing side still overlaps'):
                    record_run.require_observer_off_measured_cpus({'1': '4'})
            idle = [{'pid': 4243, 'comm': 'herdr', 'affinity': set(range(24)), 'cpu_fraction': 0.0}]
            with mock.patch.object(record_run, 'ancestor_processes', return_value=idle):
                record_run.require_observer_off_measured_cpus({'1': '4'})
            moved = [{'pid': 4244, 'comm': 'node', 'affinity': {0, 1, 2, 3, 12, 13, 14, 15},
                      'cpu_fraction': 0.24}]
            with mock.patch.object(record_run, 'ancestor_processes', return_value=moved):
                record_run.require_observer_off_measured_cpus({'1': '4'})

    def test_observer_affinity_is_recorded_not_only_refused(self):
        # A refusal that admitted a run leaves no trace of what the observer was pinned to; the
        # record states both the process's own affinity and the CPUs free of the measurement.
        with (mock.patch.object(record_run, 'l3_domain', return_value={4, 5, 6, 7, 16, 17, 18, 19}),
              mock.patch.object(record_run, 'online_cpus',
                                return_value={0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15}),
              mock.patch.object(record_run.os, 'sched_getaffinity',
                                return_value={0, 1, 2, 3, 12, 13, 14, 15})):
            record = record_run.observer_affinity({'1': '4'})
        self.assertEqual(record['affinity'], '0-3, 12-15')
        self.assertEqual(record['free_cpus'], '0-3, 12-15')   # nothing outside the domain is left out
        self.assertIn('sched_getaffinity', record['source'])

    def test_single_l3_domain_host_warns_instead_of_refusing(self):
        # A small CI runner has one L3 domain: nothing can be parked off the measured cache there,
        # so this is a recorded warning, while an observer that may land on a measured CPU still
        # refuses.
        with (mock.patch.object(record_run, 'l3_domain', return_value={4, 5}),
              mock.patch.object(record_run, 'online_cpus', return_value={4, 5}),
              mock.patch.object(record_run.os, 'sched_getaffinity', return_value={99}),
              mock.patch('sys.stderr', new_callable=io.StringIO) as loud):
            record_run.require_observer_off_measured_cpus({'1': '4'})
            self.assertIn('shares them with the measurement', loud.getvalue())

    def test_a_busy_task_outside_the_observing_side_refuses_before_the_build(self):
        # Through main(), not the helper: the point is that the gate is reached and that nothing
        # has been built by the time it refuses.
        busy = [{'pid': 1234, 'comm': 'dockerd', 'uid': os.geteuid(), 'cores': 0.5, 'tasks': 2,
                 'allowed': '0-23', 'unknown_tasks': 0}]
        built = []

        def explode(*args, **kwargs):
            built.append(args)
            raise AssertionError('build ran although the observing side was busy')

        with (mock.patch.object(record_run, 'build', side_effect=explode),
              mock.patch.object(record_run, 'boost_state', return_value={}),
              mock.patch.object(record_run.observer_audit, 'offenders', return_value=(busy, [], 0)),
              # The CI runner has one L3 domain, so the observing side is empty there and the
              # audit would correctly stand down; give it a side so the gate itself is tested.
              mock.patch.object(record_run, 'observer_cpus', return_value={0, 1}),
              mock.patch.object(record_run.os, 'sched_getaffinity', return_value={8})):
            with self.assertRaisesRegex(SystemExit, 'observer_audit --observer [0-9,-]+'):
                record_run.main(['fixture-cpu', 'fixture-kernels', '--threads', '1',
                                 '--no-publish'], root=self.root)
        self.assertEqual(built, [], 'the build ran before the observing side was checked')

    def test_the_observer_audit_names_a_process_that_may_run_on_a_measured_core(self):
        # The rule is about masks, not ancestry: a daemon the recorder never spawned, or a
        # leftover of the operator's, is what actually spoiled windows. Two busy children, one
        # confined to the observing side and one not, must be told apart.
        from campaign_toolkit import observer_audit
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            self.skipTest("needs at least two CPUs to split observing side from measured")
        observer, measured = set(cpus[:-1]), cpus[-1]
        burn = ("import os, sys, time\n"
                "os.sched_setaffinity(0, {int(sys.argv[1])})\n"
                "end = time.time() + 5\n"
                "while time.time() < end: pass\n")
        away = subprocess.Popen([sys.executable, "-c", burn, str(measured)])
        home = subprocess.Popen([sys.executable, "-c", burn, str(cpus[0])])
        try:
            time.sleep(0.4)                       # let both reach the loop
            bad, foreign, _ = observer_audit.offenders(observer, seconds=0.4)
            named = {entry['pid']: entry for entry in bad}
            self.assertIn(away.pid, named, "a process allowed on a measured core was not reported")
            self.assertEqual(named[away.pid]['allowed'], str(measured))
            self.assertNotIn(home.pid, named, "a process confined to the observing side was reported")
            self.assertEqual(foreign, [], "our own children were reported as another user's")
        finally:
            for child in (away, home):
                child.kill()
                child.wait()

    def test_host_role_defaults_to_development_and_can_be_the_measurement_host(self):
        with (mock.patch.object(record_run.platform, 'node', return_value='x'),
              mock.patch.object(record_run, 'host_info', wraps=record_run.host_info)):
            profiles = [{'name': 'p', 'cpu': 'c', 'logical_cpus': 1, 'l3': 'l', 'role': 'measurement'}]
            self.assertEqual(record_run.host_info('p', profiles)['role'], 'measurement')
            profiles[0].pop('role')
            self.assertEqual(record_run.host_info('p', profiles)['role'], 'development')

    def test_boost_on_is_refused_and_can_be_recorded(self):
        record_run.require_boost_disabled({'cpufreq_boost': '0'})              # disabled: no exit
        record_run.require_boost_disabled({})                                  # not exposed: no exit
        with self.assertRaisesRegex(SystemExit, 'boost/turbo is enabled'):
            record_run.require_boost_disabled({'cpufreq_boost': '1'})
        with self.assertRaisesRegex(SystemExit, 'boost/turbo is enabled'):
            record_run.require_boost_disabled({'intel_pstate_no_turbo': '0'})
        with mock.patch.dict(os.environ, {'FBENCH_ALLOW_BOOST': '1'}), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as loud:
            record_run.require_boost_disabled({'cpufreq_boost': '1'})
            self.assertIn('frequency level is not fixed', loud.getvalue())

    def test_descriptor_path_can_be_named(self):
        alt = self.root / "alt-campaign.yaml"
        shutil.copy(self.root / "benchmarks/campaign.yaml", alt)
        os.environ["CAMPAIGN_DESCRIPTOR"] = "alt-campaign.yaml"
        try:
            d = descriptor.load_descriptor(self.root)
        finally:
            del os.environ["CAMPAIGN_DESCRIPTOR"]
        self.assertEqual(d["id"], "fixture-campaign")
        self.assertEqual(d.path.resolve(), alt.resolve())

    def test_every_manifest_validates(self):
        manifests = sorted((self.root / "data/results").rglob("run.yaml"))
        self.assertEqual(len(manifests), 2)
        for manifest in manifests:
            self.assertEqual(validate_run.validate(str(manifest)), [], f"{manifest}")

    def test_report_publish_and_index(self):
        run_a = self.root / RUN_A_REL
        self.assertEqual(report_run.main(["--run-dir", str(run_a)], root=self.root), 0)
        self.assertTrue((run_a / "report.md").exists())

        self.assertEqual(publish_report.main([str(run_a)], root=self.root), 0)
        index = self.root / "result/INDEX.md"
        self.assertTrue(index.exists())
        self.assertIn("fixture-kernels", index.read_text())
        # The generator reproduces what publication just wrote, byte for byte.
        self.assertEqual(gen_index.main(["--check"], root=self.root), 0)

        # And rewriting it changes nothing.
        before = index.read_bytes()
        self.assertEqual(gen_index.main(["--write"], root=self.root), 0)
        self.assertEqual(index.read_bytes(), before)

    def test_index_reports_absent_checkout_only_when_something_is_recorded(self):
        # A campaign that has measured nothing has no currency to compute, so an absent
        # checkout is a note rather than a failure; once a page exists it is the reason
        # the index cannot say whether that page is current.
        root = pathlib.Path(self.tmp.name) / "empty"
        shutil.copytree(FIXTURE, root)
        shutil.rmtree(root / "extern", ignore_errors=True)
        shutil.rmtree(root / "data", ignore_errors=True)
        suite = root / SUITE_REL
        suite.write_text(suite.read_text().replace(
            "required_profiles:\n  - fixture-cpu\noptional_profiles: []",
            "required_profiles: []\noptional_profiles:\n  - fixture-cpu"))
        self.assertEqual(gen_index.main(["--write"], root=root), 0)
        self.assertEqual(gen_index.main(["--check"], root=root), 0)

    def test_dual_identity_index_names_both_revisions(self):
        # A campaign that records a baseline prints the fork identity in every line that
        # carries the revision identity, so two revisions cannot be read as one.
        import yaml as _yaml
        path = self.root / "benchmarks/campaign.yaml"
        data = _yaml.safe_load(path.read_text())
        data["identities"] = ["library", "upstream"]
        data["upstream"] = {
            "name": "fixture-upstream", "url": "https://example.invalid/upstream",
            "pin_file": "pins/fixture-rs.rev", "extern_dir": "extern/upstream",
            "pin_env": {"rev": "UP_REV", "dirty": "UP_DIRTY", "dir": "UP_DIR"},
        }
        data["page_name"] = "{library12}-{upstream12}{vendor}{dirty}"
        path.write_text(_yaml.safe_dump(data, sort_keys=False))
        descriptor.set_root(self.root)
        text = gen_index.render([], [], {"library": "a" * 40, "upstream": "b" * 40}, "none")
        self.assertIn("- Reference fork revision", text)
        self.assertIn("fork revision, upstream revision,", text)
        self.assertNotIn("library revision", text)

    def test_declared_case_axes_split_cells(self):
        # A campaign that declares an extra CSV axis must not pool two values of it: the cell
        # key carries the axis, so two orders are two cells, and a three-index shape keeps its
        # `k` rather than being read as a rectangle.
        import yaml as _yaml
        path = self.root / "benchmarks/campaign.yaml"
        data = _yaml.safe_load(path.read_text())
        data["case_axes"] = ["order"]
        data["case_label"] = "`{family}/{dims}/b{batch}` {order} {threads}T {row}"
        data["csv_columns"] = data["csv_columns"][:3] + ["order"] + data["csv_columns"][3:] + ["k"]
        path.write_text(_yaml.safe_dump(data, sort_keys=False))
        descriptor.set_root(self.root)
        row = dict(regime="r", family="f", dtype="f64", order="c", m="4", n="4", k="",
                   batch="1", row="algo-lane", threads="1")
        cell = campaign.cell_of(row)
        self.assertEqual(campaign.case_label(cell), "`f/n4/b1` c 1T algo-lane")
        self.assertNotEqual(cell, campaign.cell_of(dict(row, order="f")))
        gemm = campaign.cell_of(dict(row, m="4096", n="240", k="16"))
        self.assertEqual(campaign.case_dims(gemm.m, gemm.n, gemm.k), "4096x16x240")
        self.assertIn("4096x16x240", campaign.case_label(gemm))

    def test_run_schema_takes_a_declared_axis_and_a_three_index_shape(self):
        # The manifest schema has to accept what a dual-identity, order-axed campaign records:
        # an `order` on the run spec, and a three-index shape in a population's grid.
        import json
        import yaml as _yaml
        schema = json.loads((TOOLKIT / "campaign_toolkit/schemas/benchmark-run.schema.json")
                            .read_text())
        manifest = _yaml.safe_load((self.root / RUN_A_REL / "run.yaml").read_text())
        manifest["run_spec"]["order"] = ["c", "f"]
        manifest["run_spec"]["populations"][0]["n"] = ["4x4x4"]
        jsonschema_lite.validate(manifest, schema)

    def test_compare_runs(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = compare_runs.main([str(self.root / RUN_A_REL), str(self.root / RUN_B_REL)],
                                   root=self.root)
        self.assertEqual(rc, 0)
        out = buffer.getvalue()
        self.assertIn(" -> ", out)
        self.assertIn("- vendor arm: `openblas` on both sides", out)
        self.assertIn("## Per cell", out)
        self.assertIn("## Per family", out)

    def test_compare_refuses_two_vendor_arms(self):
        other = self.root / RUN_B_REL / "run.yaml"
        other.write_text(other.read_text().replace("name: openblas", "name: mkl", 1))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit):
            compare_runs.main([str(self.root / RUN_A_REL), str(self.root / RUN_B_REL)], root=self.root)

    def test_bad_descriptor_is_refused(self):
        bad = pathlib.Path(self.tmp.name) / "bad"
        shutil.copytree(FIXTURE, bad)
        text = (bad / "benchmarks/campaign.yaml").read_text()
        text = "\n".join(line for line in text.splitlines() if not line.startswith("page_name:"))
        (bad / "benchmarks/campaign.yaml").write_text(text)
        with self.assertRaises(SystemExit):
            descriptor.load_descriptor(bad)


    def test_profile_reference_resolves_and_is_used(self):
        # A campaign opts into a shared profile by naming it; the body comes from
        # the toolkit, so a corrected profile reaches every campaign that measures it.
        (self.root / "benchmarks/profiles.yaml").write_text(
            "schema_version: 1\nprofiles:\n  - zen5-cpu\n")
        d = descriptor.load_descriptor(self.root)
        self.assertEqual(d.profile_names(), ["zen5-cpu"])
        self.assertEqual(d.profiles()[0]["cpu"], "AMD Ryzen AI 9 HX 470")

    def test_union_index_over_one_and_two_campaigns(self):
        def publish(root):
            run_a = root / RUN_A_REL
            self.assertEqual(report_run.main(["--run-dir", str(run_a)], root=root), 0)
            self.assertEqual(publish_report.main([str(run_a)], root=root), 0)
            self.assertEqual(gen_index.main(["--write"], root=root), 0)

        publish(self.root)
        second = pathlib.Path(self.tmp.name) / "campaign2"
        shutil.copytree(self.root, second)
        campaign = second / "benchmarks/campaign.yaml"
        campaign.write_text(campaign.read_text().replace("fixture-campaign", "second-campaign"))
        publish(second)

        out = pathlib.Path(self.tmp.name) / "union.md"
        # A campaign root and an already-published INDEX.md are both inputs.
        rc = union_index.main([str(self.root), str(second / "result/INDEX.md"),
                               "--out", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text()
        self.assertIn("| campaign | suite | profile |", text)
        self.assertIn("| `fixture-campaign` | `fixture-kernels` |", text)
        self.assertIn("| `second-campaign` | `fixture-kernels` |", text)

        # One campaign is the same code path, not a special case.
        single = pathlib.Path(self.tmp.name) / "single.md"
        self.assertEqual(union_index.main([str(self.root), "--out", str(single)]), 0)
        text = single.read_text()
        self.assertIn("| `fixture-campaign` |", text)
        self.assertNotIn("second-campaign", text)


class CampaignSchemaTest(unittest.TestCase):
    """A descriptor without cargo features must load: some campaigns pick their vendor by environment."""

    def test_features_are_optional(self):
        import copy
        import json

        import yaml

        from campaign_toolkit import jsonschema_lite
        fixture = pathlib.Path(__file__).parent / 'fixture/benchmarks/campaign.yaml'
        schema = json.loads((pathlib.Path(__file__).parent.parent
                             / 'campaign_toolkit/schemas/campaign.schema.json').read_text())
        descriptor = yaml.safe_load(fixture.read_text())
        jsonschema_lite.validate(descriptor, schema)                    # the fixture, with features
        without = copy.deepcopy(descriptor)
        without['measured'].pop('features')
        jsonschema_lite.validate(without, schema)                       # accepted: vendor from the environment
        self.assertNotIn('features', schema['properties']['measured']['required'])


class VendorIdentityTest(unittest.TestCase):
    def test_vendor_identity_falls_back_to_plain_info(self):
        """A harness whose `info` takes no arguments is still usable (older campaign CLIs)."""
        calls = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            ok = command == ['/bin/b', 'info']
            return subprocess.CompletedProcess(command, 0 if ok else 2, stdout='vendor.name=mkl\n' if ok else '',
                                               stderr='' if ok else 'usage')

        with mock.patch.object(record_run.subprocess, 'run', side_effect=fake_run):
            fields = record_run.vendor_identity('/bin/b', {}, threads=4)
        self.assertEqual(fields, {'vendor.name': 'mkl'})
        self.assertEqual(calls, [['/bin/b', 'info', '--threads', '4'], ['/bin/b', 'info']])

    def test_vendor_identity_still_fails_when_neither_form_works(self):
        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 2, stdout='', stderr='usage')

        with mock.patch.object(record_run.subprocess, 'run', side_effect=fake_run):
            with self.assertRaisesRegex(SystemExit, 'info failed'):
                record_run.vendor_identity('/bin/b', {}, threads=4)


class ToolkitRepositoryTest(unittest.TestCase):
    def test_shared_profile_library(self):
        profile = descriptor.shared_profile("zen5-cpu")
        self.assertEqual(profile["name"], "zen5-cpu")
        self.assertEqual(profile["logical_cpus"], 24)
        self.assertTrue(descriptor.profiles_dir().is_dir())
        with self.assertRaises(SystemExit):
            descriptor.shared_profile("no-such-profile")

    def test_union_index_keeps_both_identities_of_a_dual_page(self):
        # A dual-identity campaign names a page by `fork/upstream`; two pages that
        # share the fork and the vendor differ only by upstream, so the whole string
        # is the identity and neither page may be dropped.
        root = pathlib.Path(tempfile.mkdtemp()) / "dual"
        index = root / "result/INDEX.md"
        index.parent.mkdir(parents=True)
        index.write_text(
            "# Result index\n\n"
            "- Reference fork revision (`pins/fork.rev`): `aaaaaaaaaaaa`\n"
            "- Reference upstream revision (`pins/up.rev`): `v1`\n"
            "- Newest recorded run: `2026-01-01`\n\n"
            "## All pages\n\n"
            "| suite | profile | vendor | fork/upstream | version | date | coverage | status | page |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| `kernels` | `cpu` | `mkl` | `aaaaaaaaaaaa/bbbbbbbbbbbb` | - | 2026-01-01 "
            "| full | current | [result/cpu/kernels/a-b-mkl.md](cpu/kernels/a-b-mkl.md) |\n"
            "| `kernels` | `cpu` | `mkl` | `aaaaaaaaaaaa/cccccccccccc` | - | 2026-01-01 "
            "| full | current | [result/cpu/kernels/a-c-mkl.md](cpu/kernels/a-c-mkl.md) |\n")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = union_index.main([str(index)])
        self.assertEqual(rc, 0)
        text = buffer.getvalue()
        self.assertIn("reference `aaaaaaaaaaaa`, upstream `v1`", text)
        self.assertIn("aaaaaaaaaaaa/bbbbbbbbbbbb", text)
        self.assertIn("aaaaaaaaaaaa/cccccccccccc", text)

    def test_package_holds_no_campaign_names(self):
        for path in sorted((TOOLKIT / "campaign_toolkit").rglob("*")):
            if path.suffix not in (".py", ".json"):
                continue
            text = path.read_text()
            for name in FORBIDDEN_IN_PACKAGE:
                self.assertNotIn(name, text, f"{path} mentions {name!r}")

    def test_top_level_schemas_match_the_packaged_copies(self):
        for packaged in sorted((TOOLKIT / "campaign_toolkit/schemas").glob("*.json")):
            top = TOOLKIT / "schemas" / packaged.name
            self.assertTrue(top.exists(), f"{top} is missing")
            self.assertEqual(top.read_bytes(), packaged.read_bytes(),
                             f"{top} has drifted from {packaged}")

    def test_schemas_are_valid_documents(self):
        from campaign_toolkit import jsonschema_lite
        for path in sorted((TOOLKIT / "campaign_toolkit/schemas").glob("*.json")):
            import json
            jsonschema_lite.check_schema(json.loads(path.read_text()))


class PublishPrFlowTest(unittest.TestCase):
    """The publish driver reaches main through a pull request; subprocess is mocked."""

    def setUp(self):
        from campaign_toolkit import publish
        self.publish = publish
        self.calls = []
        self.state = dict(branch='main', counts='0\t0', commits=1, checks=[(0, 'ok')], gh_auth=0)
        p = mock.patch.object(publish.time, 'sleep')
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(publish.subprocess, 'run', side_effect=self.fake_run)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(publish, 'dirty', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, cmd, cwd=None, capture_output=False, text=False, shell=False):
        if isinstance(cmd, str):
            return subprocess.CompletedProcess(cmd, 0, '', '')
        self.calls.append(list(cmd))
        out, code = '', 0
        if cmd[0] == 'gh':
            sub = cmd[1:3]
            if sub == ['auth', 'status']:
                code = self.state['gh_auth']
            elif sub == ['pr', 'create']:
                out = 'https://github.com/o/r/pull/7\n'
            elif sub == ['pr', 'checks']:
                checks = self.state['checks']
                code, out = checks.pop(0) if len(checks) > 1 else checks[0]
            elif sub == ['pr', 'view']:
                out = 'MERGED\n'
        elif cmd[0] == 'git':
            args = cmd[3:]
            if args[:2] == ['branch', '--show-current']:
                out = self.state['branch'] + '\n'
            elif args[:1] == ['rev-list'] and '--left-right' in args:
                out = self.state['counts'] + '\n'
            elif args[:1] == ['rev-list']:
                out = f"{self.state['commits']}\n"
            elif args[:1] == ['log']:
                out = 'publish: refresh\nsite: regenerate\n'
        return subprocess.CompletedProcess(cmd, code, out, '')

    def gh_calls(self):
        return [c for c in self.calls if c[0] == 'gh']

    def git_args(self):
        return [c[3:] for c in self.calls if c[0] == 'git']

    def campaign(self):
        return {'name': 'demo', 'root': '/nonexistent/demo'}

    def run_plan(self, *extra):
        import yaml
        plan = pathlib.Path(tempfile.mkdtemp()) / 'plan.yaml'
        plan.write_text(yaml.safe_dump({'toolkit': '/nonexistent/tk', 'campaigns': [
            {'name': 'demo', 'root': '/nonexistent/demo', 'publish': 'true'}]}))
        return self.publish.main(['--plan', str(plan), *extra])

    def test_preflight_refuses_behind_ahead_wrong_branch_and_missing_gh(self):
        cases = [dict(counts='0\t3'), dict(counts='2\t0'), dict(branch='feature'), dict(gh_auth=1)]
        for change in cases:
            self.state.update(dict(branch='main', counts='0\t0', gh_auth=0))
            self.state.update(change)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    self.publish.preflight([self.campaign()], pathlib.Path('/nonexistent/tk'))
            self.assertEqual(cm.exception.code, 1, change)
        self.state.update(dict(branch='main', counts='0\t0', gh_auth=0))
        with contextlib.redirect_stdout(io.StringIO()):
            self.publish.preflight([self.campaign()], pathlib.Path('/nonexistent/tk'))

    def test_run_branches_pushes_and_merges_with_merge_commit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_plan(), 0)
        git = self.git_args()
        switch = [a for a in git if a[:3] == ['switch', '-q', '-c']]
        self.assertEqual(len(switch), 1)
        branch = switch[0][3]
        self.assertRegex(branch, r'^publish/demo-\d{8}T\d{6}Z$')
        self.assertIn(['push', '-q', '-u', 'origin', branch], git)
        gh = self.gh_calls()
        create = next(c for c in gh if c[1:3] == ['pr', 'create'])
        self.assertIn('--base', create)
        self.assertEqual(create[create.index('--title') + 1], 'publish: refresh')
        body = create[create.index('--body') + 1]
        self.assertTrue(body.endswith('\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)'))
        self.assertIn('campaign_toolkit.publish', body)
        self.assertTrue(any(c[1:3] == ['pr', 'checks'] and '--watch' in c and '--fail-fast' in c for c in gh))
        merge = next(c for c in gh if c[1:3] == ['pr', 'merge'])
        self.assertIn('--merge', merge)
        self.assertIn('--delete-branch', merge)
        flat = [x for c in gh for x in c]
        for banned in ('--admin', '--squash', '--rebase'):
            self.assertNotIn(banned, flat)
        self.assertIn(['pull', '-q', '--ff-only', 'origin', 'main'], git)
        self.assertFalse(any(a[0] == 'push' and 'main' in a for a in git))

    def test_nothing_new_deletes_branch(self):
        self.state['commits'] = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.run_plan(), 0)
        self.assertIn('nothing new to publish', buf.getvalue())
        git = self.git_args()
        self.assertTrue(any(a[:1] == ['branch'] and '-D' in a for a in git))
        self.assertIn(['switch', '-q', 'main'], git)
        self.assertFalse(any(a[:1] == ['push'] for a in git))
        self.assertFalse(any(c[1:3] == ['pr', 'create'] for c in self.gh_calls()))

    def test_failing_checks_exit_without_merging(self):
        self.state['checks'] = [(1, 'index fail')]
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_plan()
        self.assertIn('https://github.com/o/r/pull/7', str(cm.exception.code))
        self.assertFalse(any(c[1:3] == ['pr', 'merge'] for c in self.gh_calls()))

    def test_no_checks_reported_retries_then_proceeds(self):
        self.state['checks'] = [(1, 'no checks reported on the branch')]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_plan(), 0)
        checks = [c for c in self.gh_calls() if c[1:3] == ['pr', 'checks']]
        self.assertGreater(len(checks), 1)
        self.assertTrue(any(c[1:3] == ['pr', 'merge'] for c in self.gh_calls()))

    def test_no_merge_stops_after_create(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.run_plan('--no-merge'), 0)
        gh = self.gh_calls()
        self.assertTrue(any(c[1:3] == ['pr', 'create'] for c in gh))
        self.assertFalse(any(c[1:3] == ['pr', 'checks'] for c in gh))
        self.assertFalse(any(c[1:3] == ['pr', 'merge'] for c in gh))
        self.assertIn('https://github.com/o/r/pull/7', buf.getvalue())
        self.assertEqual(self.git_args()[-1], ['switch', '-q', 'main'])

    def test_dry_run_mentions_pr_flow(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.run_plan('--dry-run'), 0)
        self.assertIn('PR to main', buf.getvalue())


if __name__ == "__main__":
    unittest.main()
