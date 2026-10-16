"""Synthetic/local tests only: no GitHub mutation or credential lookup."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

sync = load('tool_sync'); release = load('public_release')

class PublicReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pkos-release-test-'); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.repo = self.root / 'source'; self.repo.mkdir()
        sync.git(self.repo, 'init', '-b', 'main')
        sync.git(self.repo, 'config', 'user.name', 'synthetic')
        sync.git(self.repo, 'config', 'user.email', 'synthetic' + '@' + 'users.noreply.github.com')
        self.source = self.repo / 'skills/example'; self.source.mkdir(parents=True)
        files = {'SKILL.md':'---\nname: example\ndescription: Synthetic local test\n---\n# Example\n', 'README.md':'Synthetic README\n', 'LICENSE':'MIT License\n',
                 'distribution/workbuddy.json':json.dumps({'description_zh':'合成测试','description_en':'Synthetic test','version':'0.1.0','author':'synthetic'})}
        for rel, text in files.items():
            p=self.source/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        (self.source/'private').mkdir();(self.source/'private/case.md').write_text('Excluded personal example')
        sync.git(self.repo, 'add', '--', 'skills');sync.git(self.repo, 'commit', '-m', 'Synthetic fixture')
        self.data={'schema':1,'account':'synthetic','repositories':{'source':{'root':str(self.repo),'ssh_url':'git'+'@github.com:synthetic/source.git'}},
         'tools':[{'id':'TOOL-001','repository':'source','primary_paths':[str(self.source)],'public_distribution':{'repository':'example','version':'0.1.0','license':'MIT','source_directory':str(self.source),'files':list(files),'workbuddy_metadata':'distribution/workbuddy.json'}}]}
        self.registry=self.root/'ledger.md';self.registry.write_text(sync.START+'\n```json\n'+json.dumps(self.data)+'\n```\n'+sync.END)
        self.out=self.root/'candidate'
    def prepare(self):
        return release.prepare(sync,self.data,self.registry,'TOOL-001',self.out)
    def approve(self):
        r=self.prepare();p=self.root/'approval.json';p.write_text(json.dumps({'decision':'approved','approval_digest':r['approval_digest'],'target':'synthetic/example','user_confirmation_reference':'synthetic test only'}));return p,r['approval_digest']
    def test_prepare_offline_and_no_source_changes(self):
        head=sync.git(self.repo,'rev-parse','HEAD');status=sync.paths_from_status(self.repo)
        with patch.object(subprocess,'run',wraps=subprocess.run) as run:
            result=self.prepare()
            self.assertFalse(any(c.args[0][0]=='gh' for c in run.call_args_list))
        self.assertEqual(head,sync.git(self.repo,'rev-parse','HEAD'));self.assertEqual(status,sync.paths_from_status(self.repo))
        self.assertNotIn('private/case.md',[x['path'] for x in result['manifest']['files']]);self.assertFalse((self.out/'repository/.git').exists())
    def test_workbuddy_yaml_extras_and_same_body(self):
        self.prepare();asset=next((self.out/'assets').glob('*workbuddy.zip'))
        with zipfile.ZipFile(asset) as z:
            skill=z.read('SKILL.md').decode();self.assertIn('description_zh:',skill);self.assertIn('version: "0.1.0"',skill)
            self.assertEqual(skill.split('\n---\n',1)[1],(self.source/'SKILL.md').read_text().split('\n---\n',1)[1])
            self.assertFalse(any('private/' in n or '.git/' in n for n in z.namelist()))
    def test_deterministic_archives(self):
        self.prepare();release.inspect(sync,self.data,self.out)
        files,assets,*_=release.payload(sync,self.data,'TOOL-001')
        for n,raw in assets.items():self.assertEqual(raw,(self.out/'assets'/n).read_bytes())
    def test_unsafe_paths_block(self):
        for rel in ['../escape','/absolute','private/case.md','a\\b']:
            with self.assertRaises(sync.Blocked):release.regular(sync,self.source,rel)
    def test_source_symlink_block(self):
        p=self.source/'README.md';p.unlink();p.symlink_to(self.source/'LICENSE')
        with self.assertRaisesRegex(sync.Blocked,'public_symlink'):self.prepare()
    def test_secret_block_no_private_exception(self):
        p=self.source/'README.md';p.write_text('access'+'_token = "'+'x'*20+'"')
        self.data['repositories']['source']['approvals']=[{'path':'README.md','line':1,'kind':'credential-assignment','sha256':sync.digest(p.read_bytes())}]
        with self.assertRaisesRegex(sync.Blocked,'public_scan_findings'):self.prepare()
    def test_personal_path_block(self):
        (self.source/'README.md').write_text('/'+'Users/example/private/file')
        with self.assertRaisesRegex(sync.Blocked,'public_scan_findings'):self.prepare()
    def test_private_repo_url_block(self):
        (self.source/'README.md').write_text('https://github.com/synthetic/source')
        with self.assertRaisesRegex(sync.Blocked,'public_scan_findings'):self.prepare()
    def test_candidate_extra_block(self):
        self.prepare();(self.out/'extra.txt').write_text('Extra')
        with self.assertRaisesRegex(sync.Blocked,'extra_or_missing'):release.inspect(sync,self.data,self.out)
    def test_candidate_missing_block(self):
        self.prepare();(self.out/'repository/README.md').unlink()
        with self.assertRaisesRegex(sync.Blocked,'extra_or_missing'):release.inspect(sync,self.data,self.out)
    def test_candidate_tamper_block(self):
        self.prepare();(self.out/'repository/README.md').write_text('Tampered')
        with self.assertRaisesRegex(sync.Blocked,'content_changed'):release.inspect(sync,self.data,self.out)
    def test_archive_tamper_block(self):
        self.prepare();next((self.out/'assets').glob('*.zip')).write_bytes(b'changed')
        with self.assertRaisesRegex(sync.Blocked,'content_changed'):release.inspect(sync,self.data,self.out)
    def test_source_after_prepare_block(self):
        self.prepare();(self.source/'README.md').write_text('Changed after review')
        with self.assertRaisesRegex(sync.Blocked,'candidate_or_source_changed'):release.inspect(sync,self.data,self.out)
    def test_output_inside_source_block(self):
        with self.assertRaisesRegex(sync.Blocked,'inside_managed_source'):release.prepare(sync,self.data,self.registry,'TOOL-001',self.repo/'output')
    def test_existing_output_block(self):
        self.out.mkdir()
        with self.assertRaisesRegex(sync.Blocked,'output_exists'):self.prepare()
    def test_missing_approval_precedes_network(self):
        self.prepare()
        with patch.object(sync,'run',side_effect=AssertionError('Network forbidden')):
            with self.assertRaises(OSError):release.publish(sync,self.data,self.registry,self.out,self.root/'missing.json','wrong')
    def test_wrong_digest_block(self):
        p,d=self.approve()
        with self.assertRaisesRegex(sync.Blocked,'human_approval'):release.approved(sync,self.data,self.out,p,'wrong')
    def test_approval_matches_frozen_manifest(self):
        p,d=self.approve();self.assertEqual(d,release.approved(sync,self.data,self.out,p,d)['approval_digest'])
    def test_wrong_account_block(self):
        with patch.object(sync,'run',return_value='other'):
            with self.assertRaisesRegex(sync.Blocked,'account_mismatch'):release.metadata(sync,self.data,'synthetic/example')
    def test_private_remote_block(self):
        with patch.object(sync,'run',return_value='synthetic'),patch.object(subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps({'full_name':'synthetic/example','private':True}).encode(),stderr=b'')):
            with self.assertRaisesRegex(sync.Blocked,'visibility'):release.metadata(sync,self.data,'synthetic/example')
    def test_network_error_is_not_absence(self):
        with patch.object(sync,'run',return_value='synthetic'),patch.object(subprocess,'run',return_value=types.SimpleNamespace(returncode=1,stdout=b'',stderr=b'EOF')):
            with self.assertRaisesRegex(sync.Blocked,'query_failed'):release.metadata(sync,self.data,'synthetic/example')
    def test_remote_changed_stops_before_mutation(self):
        p,d=self.approve()
        with patch.object(release,'metadata',return_value={'size':1}),patch.object(release,'remote_sha',return_value='a'*40):
            with self.assertRaisesRegex(sync.Blocked,'history_changed'):release.publish(sync,self.data,self.registry,self.out,p,d)
    def gate_fixture(self):
        approval,digest=self.approve()
        root=self.root/'public';root.mkdir();sync.git(root,'init','-b','main')
        sync.git(root,'config','user.name','synthetic');sync.git(root,'config','user.email','synthetic'+'@'+'users.noreply.github.com')
        for p in (self.out/'repository').rglob('*'):
            if p.is_file():
                dest=root/p.relative_to(self.out/'repository');dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(p.read_bytes())
        sync.git(root,'add','--','.');sync.git(root,'commit','-m','Synthetic public release')
        url='git'+'@github.com:synthetic/example.git';sync.git(root,'remote','add','origin',url)
        return approval,digest,root,url
    def run_gate(self,approval,digest,root,url):
        local=sync.git(root,'rev-parse','HEAD')
        with patch.object(release,'metadata',return_value={'size':0}),patch.object(release,'remote_sha',return_value='0'*40):
            return release.gate(sync,self.data,self.out,approval,digest,root,'origin',url,'refs/heads/main '+local+' refs/heads/main '+'0'*40)
    def test_public_pre_push_success_local_objects_only(self):
        args=self.gate_fixture();self.assertEqual(self.run_gate(*args)['status'],'public_pre_push_passed')
    def test_public_pre_push_actual_tree_tamper_block(self):
        approval,digest,root,url=self.gate_fixture();(root/'README.md').write_text('Different from approved snapshot')
        sync.git(root,'add','--','README.md');sync.git(root,'commit','-m','Changed')
        with self.assertRaisesRegex(sync.Blocked,'content_changed'):self.run_gate(approval,digest,root,url)
    def test_deleted_secret_in_outgoing_history_block(self):
        approval,digest,root,url=self.gate_fixture();p=root/'gone.txt';p.write_text('access'+'_token = "'+'x'*20+'"')
        sync.git(root,'add','--','gone.txt');sync.git(root,'commit','-m','Intermediate fixture')
        p.unlink();sync.git(root,'add','--','gone.txt');sync.git(root,'commit','-m','Removed fixture')
        with self.assertRaisesRegex(sync.Blocked,'history_scan_findings'):self.run_gate(approval,digest,root,url)
    def test_public_pre_push_extra_file_block(self):
        approval,digest,root,url=self.gate_fixture();(root/'extra.txt').write_text('Extra')
        sync.git(root,'add','--','extra.txt');sync.git(root,'commit','-m','Extra file')
        with self.assertRaisesRegex(sync.Blocked,'file_set_changed'):self.run_gate(approval,digest,root,url)
    def test_empty_remote_409_requires_independent_ssh_empty(self):
        response=types.SimpleNamespace(returncode=1,stdout=json.dumps({'message':'Git Repository is empty.'}).encode(),stderr=b'HTTP 409')
        with patch.object(subprocess,'run',return_value=response),patch.object(sync,'run',return_value='') as run:
            self.assertEqual(release.remote_sha(sync,'synthetic/example',{'size':0,'ssh_url':'synthetic-remote'}),'0'*40)
            self.assertEqual(run.call_args.args[0],['git','ls-remote','synthetic-remote'])
    def test_empty_remote_409_nonempty_ssh_blocks(self):
        response=types.SimpleNamespace(returncode=1,stdout=json.dumps({'message':'Git Repository is empty.'}).encode(),stderr=b'HTTP 409')
        with patch.object(subprocess,'run',return_value=response),patch.object(sync,'run',return_value='unexpected-ref'):
            with self.assertRaisesRegex(sync.Blocked,'refs_mismatch'):release.remote_sha(sync,'synthetic/example',{'size':0,'ssh_url':'synthetic-remote'})
    def test_empty_remote_other_errors_stop(self):
        response=types.SimpleNamespace(returncode=1,stdout=b'',stderr=b'EOF')
        with patch.object(subprocess,'run',return_value=response):
            with self.assertRaisesRegex(sync.Blocked,'refs_query_failed'):release.remote_sha(sync,'synthetic/example',{'size':0,'ssh_url':'synthetic-remote'})
    def test_named_distribution_uses_its_own_manifest(self):
        tool = self.data['tools'][0]
        tool['public_distributions'] = {'capture': dict(tool['public_distribution'], repository='capture-example')}
        result = release.prepare(sync, self.data, self.registry, 'TOOL-001', self.out, 'capture')
        self.assertEqual(result['manifest']['distribution'], 'capture')
        self.assertEqual(result['manifest']['target'], 'synthetic/capture-example')
        self.assertEqual(release.inspect(sync, self.data, self.out)['approval_digest'], result['approval_digest'])

    def test_file_map_exports_canonical_source_without_copy(self):
        original = self.source/'README.md'
        (self.source/'documents').mkdir(); original.rename(self.source/'documents/README.md')
        self.data['tools'][0]['public_distribution']['file_map'] = {'README.md': 'documents/README.md'}
        result = self.prepare()
        self.assertEqual((self.out/'repository/README.md').read_text(), 'Synthetic README\n')
        self.assertFalse(original.exists())
        release.inspect(sync, self.data, self.out)

    def test_file_map_cannot_escape_source(self):
        self.data['tools'][0]['public_distribution']['file_map'] = {'README.md': '../outside'}
        with self.assertRaisesRegex(sync.Blocked, 'unsafe_path'):
            self.prepare()

    def test_private_protection_unchanged(self):
        self.assertIn("not meta.get('private')",__import__('inspect').getsource(sync.remote_metadata))

if __name__=='__main__':unittest.main()
