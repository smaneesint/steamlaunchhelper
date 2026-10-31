import ast
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

SOURCE = Path('SteamLaunchHelper_v3_6.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NAMES = {'normalize_path', 'get_steam_install_candidates', 'get_steam_accounts', 'select_steam_account', 'choose_steam_account', 'get_steam_shortcuts_path'}

class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
        self.env = dict(os=os, tk=Mock(), messagebox=Mock(), filedialog=Mock(), vdf=Mock(), _steam_shortcuts_path=None)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<detection>', 'exec'), self.env)
        self.env['get_steam_install_candidates'] = Mock(return_value=[str(self.root)])
        (self.root / 'steam.exe').touch()

    def tearDown(self):
        self.tmp.cleanup()

    def account(self, account_id):
        config = self.root / 'userdata' / str(account_id) / 'config'
        config.mkdir(parents=True)
        return str(config / 'shortcuts.vdf')

    def test_custom_install_missing_shortcuts_and_session_cache(self):
        expected = self.account(123)
        self.assertEqual(self.env['get_steam_shortcuts_path'](), expected)
        self.assertFalse(Path(expected).exists())
        self.env['get_steam_install_candidates'].return_value = []
        self.assertEqual(self.env['get_steam_shortcuts_path'](), expected)

    def test_missing_userdata_explained(self):
        with self.assertRaisesRegex(RuntimeError, 'userdata folder is missing'):
            self.env['get_steam_shortcuts_path']()

    def test_no_config_explained(self):
        (self.root / 'userdata' / '123').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'No Steam account config'):
            self.env['get_steam_shortcuts_path']()

    def test_recent_account_id_conversion(self):
        self.account(123)
        expected = self.account(456)
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'loginusers.vdf').touch()
        self.env['vdf'].load.return_value = {'users': {str(76561197960265728 + 456): {'MostRecent': '1'}}}
        self.assertEqual(self.env['get_steam_shortcuts_path'](), expected)

    def test_ambiguous_accounts_use_choice(self):
        self.account(123)
        expected = self.account(456)
        chooser = Mock(return_value=expected)
        self.env['choose_steam_account'] = chooser
        self.assertEqual(self.env['get_steam_shortcuts_path'](), expected)
        chooser.assert_called_once()

    def test_manual_install(self):
        expected = self.account(123)
        self.env['get_steam_install_candidates'].return_value = []
        self.env['filedialog'].askdirectory.return_value = str(self.root)
        self.assertEqual(self.env['get_steam_shortcuts_path'](), expected)

    def test_cancel_does_not_cache(self):
        self.env['get_steam_install_candidates'].return_value = []
        self.env['filedialog'].askdirectory.return_value = ''
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.env['get_steam_shortcuts_path']()
        self.assertIsNone(self.env['_steam_shortcuts_path'])

    def test_library_folder_rejected(self):
        self.env['get_steam_install_candidates'].return_value = []
        library = self.root / 'SteamLibrary'
        library.mkdir()
        self.env['filedialog'].askdirectory.return_value = str(library)
        with self.assertRaisesRegex(RuntimeError, 'does not contain steam.exe'):
            self.env['get_steam_shortcuts_path']()

    def test_registry_candidates_and_deduplication(self):
        registry = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, KEY_WOW64_32KEY=4, KEY_WOW64_64KEY=8, KEY_READ=16)
        registry.OpenKey = MagicMock()
        registry.QueryValueEx = Mock(return_value=(str(self.root), 1))
        node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'get_steam_install_candidates')
        scope = dict(self.env)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<registry>', 'exec'), scope)
        with patch.dict('sys.modules', winreg=registry):
            result = scope['get_steam_install_candidates']()
        self.assertEqual(result[0], str(self.root))
        self.assertEqual(result.count(str(self.root)), 1)
        self.assertEqual(registry.QueryValueEx.call_count, 4)

    def test_unrelated_functions_unchanged(self):
        original = ast.parse(Path('SteamLaunchHelper_v3_5.py').read_text(encoding='utf-8'))
        before = {n.name: ast.dump(n) for n in original.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        after = {n.name: ast.dump(n) for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed, {'get_steam_shortcuts_path', 'SteamLaunchHelperApp'})

if __name__ == '__main__':
    unittest.main(verbosity=2)
