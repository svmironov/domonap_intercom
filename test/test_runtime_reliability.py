"""Regression tests for account isolation, cleanup, cache and HTTP ownership."""
import asyncio
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_panel_call_flow as support
from custom_components.domonap.api import IntercomAPI
from custom_components.domonap import relay, util, diagnostics
from custom_components.domonap.button import IntercomDoor
from homeassistant.exceptions import HomeAssistantError


class KeyCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_platforms_share_all_pages_and_get_independent_copies(self):
        api = IntercomAPI()
        async def page(**kwargs):
            await asyncio.sleep(0)
            number = kwargs['current_page']
            return {'results': [{'id': str(number), 'doorId': str(number)}], 'pageCount': 2}
        api.get_paged_keys = AsyncMock(side_effect=page)
        results = await asyncio.gather(*(api.get_keys() for _ in range(5)))
        self.assertEqual(api.get_paged_keys.await_count, 2)
        self.assertEqual([key['id'] for key in results[0]['results']], ['1', '2'])
        results[0]['results'][0]['doorId'] = 'modified'
        self.assertEqual((await api.get_keys())['results'][0]['doorId'], '1')
        await api.get_keys()
        self.assertEqual(api.get_paged_keys.await_count, 2)

    async def test_failure_on_later_page_does_not_publish_partial_cache(self):
        api = IntercomAPI()
        api.get_paged_keys = AsyncMock(side_effect=[
            {'results': [{'id': '1'}], 'pageCount': 2}, {'error': 'HTTP 500'}])
        self.assertIn('error', await api.get_keys())
        self.assertIsNone(api._keys_cache)

    async def test_repeated_page_stops_without_unbounded_requests(self):
        api = IntercomAPI()
        api.get_paged_keys = AsyncMock(return_value={'results': [{'id': '1'}], 'pageCount': 100})
        self.assertIn('error', await api.get_keys())
        self.assertEqual(api.get_paged_keys.await_count, 2)

    async def test_cache_is_kept_until_reload_and_accounts_are_isolated(self):
        first, second = IntercomAPI(), IntercomAPI()
        for api, key in ((first, 'first'), (second, 'second')):
            api.get_paged_keys = AsyncMock(return_value={'results': [{'id': key}], 'pageCount': 1})
        await first.get_keys()
        self.assertEqual((await second.get_keys())['results'][0]['id'], 'second')
        await first.get_keys()
        self.assertEqual(first.get_paged_keys.await_count, 1)
        await first.close()
        self.assertIsNone(first._keys_cache)


class RelayCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_and_service_propagate_network_failure_after_cleanup(self):
        for use_button in (True, False):
            with self.subTest(button=use_button):
                hass = support.FakeHass()
                api = IntercomAPI()
                api.config_entry_id = 'account'
                api.set_active_call('old')
                api.open_relay_by_key_id = AsyncMock(side_effect=TimeoutError())
                api.end_active_call = AsyncMock(return_value={'ok': True})
                if use_button:
                    button = IntercomDoor(api, None, 'key', 'door', 'Door', {}, unique_id='door')
                    button.hass = hass
                    action = button.async_press
                else:
                    hass.data['domonap'] = {'account': {'api': api}}
                    await support.actions.async_setup_actions(hass)
                    handler = hass.services.registered[('domonap', 'open_relay_by_key_id')]
                    action = lambda: handler(SimpleNamespace(data={'key_id': 'key'}))
                with self.assertRaises(HomeAssistantError):
                    await action()
                api.end_active_call.assert_awaited_once_with(expected_call_id='old')
                self.assertEqual(hass.bus.events[0][1]['config_entry_id'], 'account')

    async def test_new_phone_call_survives_relay_completion(self):
        api = IntercomAPI()
        api.set_active_call('old')
        async def opening(_):
            api.set_active_call('new')
            return {'ok': True}
        api.open_relay_by_key_id = opening
        api.end_call_notify = AsyncMock()
        result = await relay.open_relay(support.FakeHass(), api, None, 'key', by_door=False, source='test')
        self.assertEqual(api.active_call_id, 'new')
        self.assertTrue(result['end_call_result']['skipped'])
        api.end_call_notify.assert_not_awaited()

    async def test_external_dtmf_opens_and_ends_both_dialogs(self):
        api = support.RubetekPanelIntercomAPI(instance_id="0123456789abcdef")
        api.set_active_call("call")
        api.get_keys = AsyncMock(return_value={"results": [{"id": "key", "doorId": "door"}]})
        api._post = AsyncMock(return_value="")
        api._answer_active_sip_before_open = AsyncMock()
        api.end_active_call = AsyncMock(return_value={"ok": True})
        external = SimpleNamespace(established=True, hangup=AsyncMock())
        controller = support.PanelCallController(support.FakeHass(), api, config_entry_id="entry", enabled=True)
        controller._active_call_id = "call"
        controller._active_door_id = "door"
        controller._account = SimpleNamespace(active_call=external)
        await controller._on_external_dtmf("1")
        api._post.assert_awaited_once()
        self.assertEqual(api._post.await_args.args[1], {"keyId": "key"})
        api._answer_active_sip_before_open.assert_not_awaited()
        api.end_active_call.assert_awaited_once_with(expected_call_id="call")
        external.hangup.assert_awaited_once_with(local=True)

    async def test_cancellation_cleans_up_and_propagates(self):
        api = IntercomAPI()
        api.set_active_call('call')
        entered = asyncio.Event()
        async def opening(_):
            entered.set()
            await asyncio.Event().wait()
        api.open_relay_by_key_id = opening
        api.end_active_call = AsyncMock(return_value={'ok': True})
        task = asyncio.create_task(relay.open_relay(support.FakeHass(), api, None, 'key', by_door=False, source='test'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        api.end_active_call.assert_awaited_once()


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_401_is_released_before_refresh_and_retry_uses_tls_defaults(self):
        api = IntercomAPI()
        api.refresh_token = 'secret'
        first = SimpleNamespace(status=401, release=Mock())
        second = SimpleNamespace(status=200, release=Mock(), json=AsyncMock(return_value={'ok': True}))
        session = SimpleNamespace(post=AsyncMock(side_effect=[first, second]))
        api._ensure_session = AsyncMock(return_value=session)
        async def refresh(_):
            self.assertTrue(first.release.called)
            return True
        api._refresh_for_retry = refresh
        self.assertEqual(await api._post('/test'), {'ok': True})
        self.assertTrue(second.release.called)
        for call in session.post.await_args_list:
            self.assertIsNot(call.kwargs.get('ssl'), False)

    async def test_response_is_released_on_decode_error(self):
        api = IntercomAPI()
        response = SimpleNamespace(status=200, release=Mock(), json=AsyncMock(side_effect=ValueError('secret')))
        api._ensure_session = AsyncMock(return_value=SimpleNamespace(post=AsyncMock(return_value=response)))
        with self.assertRaises(ValueError):
            await api._post('/test')
        response.release.assert_called_once()
        self.assertEqual(api.runtime_status.last_error, 'http: ValueError')


class AccountTests(unittest.TestCase):
    def test_ambiguous_account_requires_explicit_selection(self):
        hass = support.FakeHass()
        hass.data['domonap'] = {'one': {'api': IntercomAPI()}, 'two': {'api': IntercomAPI()}}
        with self.assertRaises(HomeAssistantError):
            support.actions._select_entry_id(hass, None)
        self.assertEqual(support.actions._select_entry_id(hass, 'two'), 'two')

    def test_renamed_sensor_is_resolved_and_foreign_sensor_rejected(self):
        entry = SimpleNamespace(entry_id='one', title='Account', data={'auth_mode': 'panel'})
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda _: entry))
        registry = SimpleNamespace(async_get_entity_id=Mock(return_value='sensor.renamed'),
                                   async_get=Mock(return_value=SimpleNamespace(config_entry_id='one')))
        with patch.object(util.er, 'async_get', return_value=registry):
            self.assertEqual(util.find_last_call_sensor_entity_id(hass, 'one'), 'sensor.renamed')
            registry.async_get.return_value.config_entry_id = 'two'
            self.assertIsNone(util.find_last_call_sensor_entity_id(hass, 'one'))
        self.assertNotEqual(util.scoped_device_id('one', 'door'), util.scoped_device_id('two', 'door'))

    def test_phone_events_are_filtered_by_account(self):
        self.assertFalse(util.event_belongs_to_entry({'config_entry_id': 'two'}, 'one', panel_scoped=False))
        self.assertTrue(util.event_belongs_to_entry({'config_entry_id': 'one'}, 'one', panel_scoped=False))

    def test_single_owner_device_preserves_id_and_migration_is_idempotent(self):
        from homeassistant.helpers import device_registry as dr
        device = SimpleNamespace(id='existing', identifiers={('domonap', 'key')}, config_entries={'one'})
        def update(_, **kwargs):
            device.identifiers = kwargs['new_identifiers']
        devices = SimpleNamespace(async_update_device=Mock(side_effect=update))
        with patch.object(dr, 'async_get', return_value=devices), \
             patch.object(dr, 'async_entries_for_config_entry', return_value=[device]), \
             patch.object(util.er, 'async_get', return_value=Mock()):
            util.migrate_device_identifiers(None, SimpleNamespace(entry_id='one'))
            util.migrate_device_identifiers(None, SimpleNamespace(entry_id='one'))
        devices.async_update_device.assert_called_once_with('existing', new_identifiers={('domonap', 'entry:one:key')})

    def test_shared_device_is_split_and_entities_rebound(self):
        from homeassistant.helpers import device_registry as dr
        device = SimpleNamespace(id='shared', identifiers={('domonap', 'key')}, config_entries={'one', 'two'},
                                 name='Door', manufacturer='Domonap', model='Intercom', area_id='hall',
                                 name_by_user='My door', labels=set(), disabled_by=None)
        devices = SimpleNamespace(async_get_or_create=Mock(return_value=SimpleNamespace(id='split')),
                                  async_update_device=Mock())
        entities = SimpleNamespace(async_update_entity=Mock())
        entity = SimpleNamespace(entity_id='button.my_door', device_id='shared', platform='domonap')
        with patch.object(dr, 'async_get', return_value=devices), \
             patch.object(dr, 'async_entries_for_config_entry', return_value=[device]), \
             patch.object(util.er, 'async_get', return_value=entities), \
             patch.object(util.er, 'async_entries_for_config_entry', return_value=[entity]):
            util.migrate_device_identifiers(None, SimpleNamespace(entry_id='one'))
        self.assertEqual(devices.async_get_or_create.call_args.kwargs['identifiers'], {('domonap', 'entry:one:key')})
        entities.async_update_entity.assert_called_once_with('button.my_door', device_id='split')
        devices.async_update_device.assert_any_call('shared', remove_config_entry_id='one')


class UnloadAndDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_platform_unload_preserves_runtime(self):
        module = types.ModuleType('custom_components.domonap')
        module.__package__ = 'custom_components.domonap'
        path = support.PKG / '__init__.py'
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
        runtime = {'api': SimpleNamespace(close=AsyncMock()), 'notify_consumer': SimpleNamespace(stop=AsyncMock())}
        hass = SimpleNamespace(data={'domonap': {'entry': runtime}},
                               config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=False)))
        self.assertFalse(await module.async_unload_entry(hass, SimpleNamespace(entry_id='entry')))
        self.assertIs(hass.data['domonap']['entry'], runtime)
        runtime['api'].close.assert_not_awaited()
        runtime['notify_consumer'].stop.assert_not_awaited()

    async def test_token_updates_do_not_reload_or_refetch_doors(self):
        module = types.ModuleType('custom_components.domonap')
        module.__package__ = 'custom_components.domonap'
        path = support.PKG / '__init__.py'
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
        hass = SimpleNamespace(data={'domonap': {'entry': {'options': {}}}},
                               config_entries=SimpleNamespace(async_reload=AsyncMock()))
        entry = SimpleNamespace(entry_id='entry', options={}, data={'access_token': 'refreshed'})
        await module._async_update_listener(hass, entry)
        hass.config_entries.async_reload.assert_not_awaited()
        entry.options = {'call_end_mode': 'reject'}
        await module._async_update_listener(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with('entry')

    async def test_successful_unload_closes_sessions_before_api_and_keeps_services(self):
        module = types.ModuleType('custom_components.domonap')
        module.__package__ = 'custom_components.domonap'
        path = support.PKG / '__init__.py'
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
        events = []
        api = SimpleNamespace(close=AsyncMock(side_effect=lambda: events.append('api')))
        proxy = SimpleNamespace(close_for_api=AsyncMock(side_effect=lambda _: events.append('video')))
        hass = SimpleNamespace(
            data={'domonap': {'entry': {'api': api}, 'webrtc_proxy': proxy}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
            services=SimpleNamespace(async_remove=Mock()),
        )
        self.assertTrue(await module.async_unload_entry(hass, SimpleNamespace(entry_id='entry')))
        self.assertEqual(events, ['video', 'api'])
        self.assertNotIn('entry', hass.data['domonap'])
        hass.services.async_remove.assert_not_called()

    async def test_diagnostics_exclude_credentials_and_event_contents(self):
        api = IntercomAPI()
        api.access_token = 'secret-token'
        api.runtime_status.event_received()
        api.runtime_status.failed('signalr', 'timeout')
        hass = SimpleNamespace(data={'domonap': {'entry': {'api': api, 'notify_consumer': SimpleNamespace(connected=True)}}})
        result = await diagnostics.async_get_config_entry_diagnostics(hass, SimpleNamespace(entry_id='entry'))
        self.assertEqual(result['signalr'], 'connected')
        self.assertEqual(result['sip'], 'idle')
        self.assertEqual(result['last_error'], 'signalr: timeout')
        self.assertNotIn('secret-token', repr(result))
        self.assertIsInstance(result['last_event'], str)


if __name__ == '__main__':
    unittest.main()
