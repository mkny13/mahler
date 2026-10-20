"""Project-bound issue tools for D43. No engine, shell tool or lease operations.

The trusted worker supplies the session, turn and fencing token, never the model.
Mutation IDs are caller-visible stable keys; their unguessable remote markers and
receipts are server-owned. An uncertain write is only looked up, never reissued.
"""

import json
from pathlib import PurePosixPath
import subprocess

from . import config
from .gh import AGENT_NOTE, GHError, LABEL_STATES, label_names, project_client, trusted_actor

MAX_FILE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 512 * 1024

# Explicit schemas also reject extra keys at dispatch (e.g. add_item.claim).
TOOL_FIELDS = {
    'read_repo_file': ({'path': 'string'}, ('path',)),
    'list_repo_files': ({}, ()),
    'list_items': ({}, ()),
    'get_item': ({'number': 'integer'}, ('number',)),
    'add_item': ({'operation_id': 'string', 'title': 'string', 'body': 'string'},
                 ('operation_id', 'title', 'body')),
    'edit_item': ({'operation_id': 'string', 'number': 'integer', 'expected_version': 'string',
                  'refinement': 'string'}, ('operation_id', 'number', 'expected_version', 'refinement')),
    'answer_item': ({'operation_id': 'string', 'number': 'integer', 'answer': 'string'},
                    ('operation_id', 'number', 'answer')),
}
DESCRIPTIONS = {
    'read_repo_file': 'Read a bounded tracked UTF-8 text file from the repository HEAD.',
    'list_repo_files': 'List bounded tracked regular files in the repository HEAD.',
    'list_items': 'List open admitted issues in the bound project scope; untrusted issues and comments are server-filtered.',
    'get_item': 'Read an admitted issue, its server-filtered trusted comments and its version for edit_item.',
    'add_item': 'Create an issue. Reuse operation_id and identical arguments on retry.',
    'edit_item': 'Append a refinement preserving existing text and labels; requires get_item version.',
    'answer_item': 'Save an answer as an agent-marked issue comment. Reuse operation_id on retry.',
}


def tool_definitions():
    return [{'name': name, 'description': DESCRIPTIONS[name], 'inputSchema': {
        'type': 'object', 'properties': {key: {'type': kind} for key, kind in fields.items()},
        'required': list(required), 'additionalProperties': False}}
        for name, (fields, required) in TOOL_FIELDS.items()]


class DiscussionTools:
    def __init__(self, cfg, led, session_id, turn_id, worker_token):
        self.cfg, self.led = cfg, led
        self.session_id, self.turn_id, self.worker_token = session_id, turn_id, worker_token
        self.session = led.chat_session(session_id)
        self._check_binding()
        self.gh = project_client(cfg, self.pol)
        self._logins = None

    def _check_binding(self):
        project = self.session['project']
        if project not in self.cfg.get('projects', {}):
            raise ValueError('binding_mismatch: unknown project')
        self.pol = config.project_policy(self.cfg, project)
        if (self.pol.get('enabled') is False or self.pol.get('repo') != self.session['repo']
                or self.session['account'] not in config.accounts_of(self.pol)):
            raise ValueError('binding_mismatch: project configuration changed')
        row = self.led.check_chat_worker(self.turn_id, self.worker_token)
        if row['session_id'] != self.session_id:
            raise ValueError('binding_mismatch: turn belongs to another session')

    def call(self, name, args):
        self._check_binding()
        if name not in TOOL_FIELDS:
            raise ValueError('tool not allowed')
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
        # Clients may repeat the bound project, but cannot choose another one.
        args = dict(args)
        if args.pop('project', self.session['project']) != self.session['project']:
            raise ValueError('cross-project access denied')
        fields, required = TOOL_FIELDS[name]
        if set(args) - fields.keys() or set(required) - args.keys():
            raise ValueError('unexpected or missing tool arguments')
        for key, value in args.items():
            if fields[key] == 'integer':
                if type(value) is not int or value < 1:
                    raise ValueError('issue number must be a positive integer')
            elif not isinstance(value, str) or len(value) > 64000 or (key != 'body' and not value.strip()):
                raise ValueError('invalid text argument')
        if name == 'list_repo_files':
            return list(self._tracked_files())
        if name == 'read_repo_file':
            return self._read_file(args['path'])
        if name == 'list_items':
            issues = [self._visible(i) for i in self.gh.open_issues()
                      if self._in_scope(i) and self._admitted(i)]
            return self._bounded(issues)
        if name == 'get_item':
            return self._bounded(self._visible(self._issue(args['number'])))
        return self._mutate(name, args)

    @staticmethod
    def _bounded(value):
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError('repository context exceeds size limit')
        return value

    def _in_scope(self, issue):
        return self.pol.get('scope') != 'label' or self.pol['scope_label'] in label_names(issue)

    def _trusted_logins(self):
        # D46: the repository owner and the configured GitHub identity. A failed
        # identity lookup never widens trust.
        if self._logins is None:
            self._logins = (self.session['repo'].split('/', 1)[0],)
            try:
                self._logins += (self.gh.authenticated_login(),)
            except GHError:
                pass
        return self._logins

    def _trusted(self, login, association):
        return trusted_actor(login, association, self._trusted_logins())

    @staticmethod
    def _login(actor):
        return actor.get('login') if isinstance(actor, dict) else actor

    def _admitted(self, issue):
        """D46 issue admission: a trusted author, or the collaborator-set scope label."""
        scope_label = self.pol.get('scope_label', 'mahler')
        if scope_label not in LABEL_STATES and scope_label in label_names(issue):
            return True
        login = issue.get('authorLogin') or self._login(issue.get('author'))
        return self._trusted(login, issue.get('authorAssociation'))

    def _visible(self, issue):
        """The model's view: only D46-trusted comments; raw issues stay server-side."""
        issue = dict(issue)
        issue['comments'] = [c for c in issue.get('comments', [])
                             if self._trusted(self._login(c.get('author')), c.get('authorAssociation'))]
        return issue

    def _issue(self, number):
        issue = self.gh.discussion_issue(number)
        if not self._in_scope(issue):
            raise ValueError('issue outside project scope')
        if not self._admitted(issue):
            raise ValueError('issue not admitted: untrusted author without scope-label opt-in')
        if issue.get('url') != self._url(number):
            raise ValueError('unconfirmed or cross-repository issue URL')
        return issue

    def _url(self, number):
        return f"https://github.com/{self.session['repo']}/issues/{number}"

    def _git(self, *args, limit=MAX_CONTEXT_BYTES):
        # Fixed read-only git commands, never a user-supplied command or pathspec.
        with subprocess.Popen(['git', '-C', self.pol['path'], *args], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as proc:
            try:
                data = proc.stdout.read(limit + 1)
                if len(data) > limit:
                    raise ValueError('repository context exceeds size limit')
                if proc.wait(timeout=10):
                    raise ValueError('cannot read tracked repository context')
                return data
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def _tracked_files(self):
        entries = self._git('ls-tree', '-r', '-z', '--full-tree', 'HEAD')
        files = {}
        for entry in entries.split(b'\0'):
            if not entry:
                continue
            metadata, path = entry.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            if mode in (b'100644', b'100755') and kind == b'blob':
                files[path.decode('utf-8')] = oid.decode('ascii')
        return files

    def _read_file(self, path):
        parts = PurePosixPath(path).parts
        if (path.startswith('/') or '\\' in path or '\0' in path or
                any(p in ('..', '.') for p in path.split('/')) or not parts):
            raise ValueError('path escape denied')
        oid = self._tracked_files().get(path)
        if not oid:
            raise ValueError('path is not a tracked regular file (symlinks denied)')
        data = self._git('cat-file', 'blob', oid, limit=MAX_FILE_BYTES)
        if b'\0' in data:
            raise ValueError('binary repository file denied')
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('repository file is not UTF-8 text') from None

    def _before_edit(self, issue):
        self._check_binding()
        if not self._in_scope(issue):
            raise ValueError('issue outside project scope')
        if not self._admitted(issue):
            raise ValueError('issue not admitted: untrusted author without scope-label opt-in')

    def _mutate(self, name, args):
        operation_id = args['operation_id']
        payload = {k: v for k, v in args.items() if k != 'operation_id'}
        # Validate scope before reserving a new operation. No label changes are
        # exposed: existing Mahler labels and an already-present scope survive.
        issue = self._issue(args['number']) if name != 'add_item' else None
        mutation, fresh = self.led.begin_chat_mutation(
            self.turn_id, self.worker_token, operation_id, name, payload)
        if mutation['status'] == 'confirmed':
            return {'operation_id': operation_id, 'issue_url': mutation['issue_url'], 'confirmed': True}
        marker = mutation['marker']
        url = None
        if not fresh:
            url = self._reconcile(name, args, marker, issue)
            if not url:
                raise GHError('uncertain: previous write not confirmed; do not repeat with a new operation ID')
        else:
            # Once this durable intent exists, even a process death before send
            # is uncertain. Availability never takes precedence over deduplication.
            self._check_binding()
            try:
                if name == 'add_item':
                    labels = [self.pol['scope_label']] if self.pol.get('scope') == 'label' else []
                    url = self.gh.create_issue(args['title'], args['body'] + '\n\n' + marker, labels)
                elif name == 'answer_item':
                    self.gh.comment(args['number'], AGENT_NOTE + '\n' + args['answer'] + '\n\n' + marker)
                    url = issue['url']
                else:
                    url = self.gh.discussion_edit(args['number'], args['expected_version'],
                                                  args['refinement'], marker, before_write=self._before_edit)
            except GHError:
                url = self._reconcile(name, args, marker)
                if not url:
                    raise
        self.led.confirm_chat_mutation(self.session_id, operation_id, url)
        return {'operation_id': operation_id, 'issue_url': url, 'confirmed': True}

    def _reconcile(self, name, args, marker, issue=None):
        if name == 'add_item':
            return self.gh.issue_by_marker(marker)
        issue = issue or self._issue(args['number'])
        if name == 'answer_item':
            if any(c.get('body', '').startswith(AGENT_NOTE + '\n') and marker in c['body']
                   for c in issue['comments']):
                return issue['url']
        elif marker in issue['body']:
            return issue['url']
        return None
