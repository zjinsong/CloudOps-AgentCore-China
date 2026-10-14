import io,json,os,unittest,zipfile
from types import SimpleNamespace
from unittest.mock import Mock,patch
os.environ['AWS_EC2_METADATA_DISABLED']='true'
from control import security,terraform,registry
from control.store import Store
from scripts.deploy import zip_function,raw_fields,TARGETS
from scripts.permissions import READ_ACTIONS

class ReferenceTests(unittest.TestCase):
    def test_empty_password_never_authenticates(self):
        with patch.dict(os.environ,{'CLOUDOPS_USERS':'','ADMIN_PASSWORD':''}):
            self.assertIsNone(security.authenticate('admin',''))
    def test_invalid_session_secret_fails_startup(self):
        with patch.dict(os.environ,{'SESSION_SECRET':'short'}):
            with self.assertRaises(RuntimeError):security.validate_auth_config()
    def test_apply_is_disabled_before_any_record_read(self):
        with patch.dict(os.environ,{'CLOUDOPS_ENABLE_APPLY':'false'}),patch.object(terraform,'store') as db:
            with self.assertRaises(PermissionError):terraform.apply('any')
            db.get.assert_not_called()
    def test_seeded_routines_are_disabled_by_default(self):
        with patch.dict(os.environ,{'CLOUDOPS_ENABLE_DEFAULT_ROUTINES':'false'}),patch.object(registry,'store',Store()) as db:
            registry.seed_routines()
            self.assertTrue(db.all('routine'))
            self.assertFalse(any(r['enabled'] for r in db.all('routine')))
    def test_business_actions_do_not_grant_mutations(self):
        for actions in READ_ACTIONS.values():
            for action in actions:
                self.assertTrue(action.split(':')[1].startswith(('Get','List','Describe','Lookup','Filter')),action)
    def test_lambda_packages_cover_all_targets(self):
        for name in TARGETS:
            with zipfile.ZipFile(io.BytesIO(zip_function(name))) as package:
                self.assertIn('handler.py',package.namelist())
                compile(package.read('handler.py'),name,'exec')
    def test_old_sdk_preserves_raw_platform_version(self):
        cp=Mock();raw_fields(cp)
        callback=cp.meta.events.register.call_args.args[1];parsed={}
        callback(SimpleNamespace(content=b'{"platformVersion":"V2"}'),parsed)
        self.assertEqual(parsed['platformVersion'],'V2')

if __name__=='__main__':unittest.main()
