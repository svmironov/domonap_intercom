import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_panel_call_flow  # noqa: F401 - bootstrap the integration package
from aiohttp import web
from custom_components.domonap import camera, image, webrtc_proxy


class ImageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_image_cannot_replace_new_image_and_unload_waits(self):
        with patch.object(image.ImageEntity, '__init__', return_value=None):
            entity = image.IntercomCallImageEntity(
                None, None, 'key', 'door', 'Door', entry_id='entry',
                panel_scoped=True, unique_id='image')
        entity.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        entity._set_image = AsyncMock()
        started = asyncio.Event()
        async def fetch(url, **kwargs):
            if url == 'old':
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Even a transport that returns after cancellation must not
                    # overwrite the newer incoming call's image.
                    return b'old'
            return b'new'
        entity._http_get_bytes = fetch
        entity._start_fetch('old')
        await started.wait()
        entity._start_fetch('new')
        await entity._fetch_task
        entity._set_image.assert_awaited_once_with(b'new')
        await entity.async_will_remove_from_hass()
        self.assertIsNone(entity._fetch_task)
        entity._start_fetch('after-unload')
        self.assertIsNone(entity._fetch_task)


class VideoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_camera(self, api):
        entity = camera.IntercomWebRTCCamera(
            api, 'key', 'Door', None, None,
            {'webrtcVideoUrl': 'https://video.example/stream'})
        entity.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        return entity

    async def test_unload_closes_existing_and_in_flight_camera_sessions(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def create(*_):
            entered.set()
            await release.wait()
            return {'ok': True, 'location': '/new-session', 'answer_sdp': 'answer'}
        api = SimpleNamespace(create_whep_session=create, close_whep_session=AsyncMock(return_value={'ok': True}))
        entity = self.make_camera(api)
        entity._webrtc_sessions['old'] = camera.WHEPSession('https://video.example/old', None)
        send = Mock()
        # HA versions differ in their answer/error dataclasses; protocol behavior
        # is tested independently of their constructors.
        with patch.object(camera, 'WebRTCAnswer', lambda value: value), \
             patch.object(camera, 'WebRTCError', Mock()), \
             patch.object(camera.Camera, 'async_will_remove_from_hass', new=AsyncMock()):
            offer = asyncio.create_task(entity.async_handle_async_webrtc_offer('v=0\r\n', 'new', send))
            await entered.wait()
            unload = asyncio.create_task(entity.async_will_remove_from_hass())
            await asyncio.sleep(0)
            self.assertFalse(unload.done())
            release.set()
            await asyncio.gather(offer, unload)
        urls = {call.args[0] for call in api.close_whep_session.await_args_list}
        self.assertEqual(urls, {'https://video.example/old', 'https://video.example/new-session'})
        self.assertFalse(entity._webrtc_sessions)
        self.assertFalse(entity._pending_candidates)
        send.assert_not_called()

    async def test_proxy_unload_waits_for_inflight_offer_and_closes_upstream(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def create(*_):
            entered.set()
            await release.wait()
            return {'ok': True, 'location': '/new', 'answer_sdp': 'answer'}
        api = SimpleNamespace(create_whep_session=create, close_whep_session=AsyncMock(return_value={'ok': True}))
        proxy = webrtc_proxy.DomonapWebRTCProxy(None)
        proxy.register_camera('secret', 'camera', api, 'https://video.example/whep')
        offer = asyncio.create_task(proxy.create_session('secret', 'camera', 'offer'))
        await entered.wait()
        unload = asyncio.create_task(proxy.close_for_api(api))
        await asyncio.sleep(0)
        self.assertFalse(unload.done())
        release.set()
        with self.assertRaises(web.HTTPNotFound):
            await offer
        await unload
        api.close_whep_session.assert_awaited_once_with('https://video.example/new')
        self.assertFalse(proxy._sessions)
        self.assertFalse(proxy._targets)

    async def test_camera_exposes_source_id_and_fetches_archive(self):
        api = SimpleNamespace(get_video_archive=AsyncMock(return_value={"ok": True, "playbackUrl": "https://video.example/get"}))
        entity = camera.IntercomWebRTCCamera(
            api, 'key-1', 'Door 1', None, None,
            {'webrtcVideoUrl': 'https://video.example/stream', 'sourceId': 'source-123'})
        self.assertEqual(entity.source_id, 'source-123')
        self.assertEqual(entity.extra_state_attributes.get('source_id'), 'source-123')
        archive = await entity.async_get_video_archive()
        self.assertEqual(archive, {"ok": True, "playbackUrl": "https://video.example/get"})
        api.get_video_archive.assert_awaited_once_with('source-123')

    def test_whep_url_from_webrtc_url_handles_trailing_whep(self):
        self.assertEqual(
            camera._whep_url_from_webrtc_url("https://video.example/live/stream"),
            "https://video.example/live/stream/whep",
        )
        self.assertEqual(
            camera._whep_url_from_webrtc_url("https://video.example/live/stream/whep"),
            "https://video.example/live/stream/whep",
        )
        self.assertEqual(
            camera._whep_url_from_webrtc_url("https://video.example/live/stream/whep?token=123"),
            "https://video.example/live/stream/whep?token=123",
        )

    async def test_async_setup_entry_merges_main_and_active_keys_and_resolves_archive(self):
        # Main has 0 keys (e.g. user didn't favorite in app), Active has 1 key with sourceId only
        api = SimpleNamespace(
            config_entry_id="entry-1",
            get_keys=AsyncMock(side_effect=lambda keys_type="Active": {
                "results": [] if keys_type == "Main" else [
                    {"id": "active-door-1", "name": "Active Door", "sourceId": "src-456"}
                ]
            }),
            get_video_archive=AsyncMock(return_value={
                "webRtcUrl": "https://video.example/active/stream",
                "hlsUrl": "https://video.example/active/hls.m3u8",
            }),
            get_video_area=AsyncMock(return_value=[]),
        )
        config_entry = SimpleNamespace(entry_id="entry-1", data={})
        hass = SimpleNamespace(
            data={
                "domonap": {
                    "entry-1": {"api": api},
                    "webrtc_proxy": None,
                }
            }
        )
        added_entities = []
        def async_add_entities(entities, update):
            added_entities.extend(entities)

        result = await camera.async_setup_entry(hass, config_entry, async_add_entities)
        self.assertTrue(result)
        self.assertEqual(len(added_entities), 1)
        self.assertEqual(added_entities[0]._name, "Active Door")
        self.assertEqual(added_entities[0]._source_id, "src-456")
        self.assertEqual(added_entities[0]._stream_url, "https://video.example/active/hls.m3u8")

    async def test_build_video_camera_entities_loads_folders_and_categories_and_camera_ids(self):
        api = SimpleNamespace(
            config_entry_id="entry-1",
            get_folder_video_cameras=AsyncMock(return_value=[
                {
                    "id": "cam-folder-1",
                    "name": "Folder Camera",
                    "httpVideoUrl": "https://video.example/folder/cam.m3u8",
                }
            ]),
            get_user_video_cameras=AsyncMock(return_value=[
                {
                    "id": "cam-cat-1",
                    "name": "Category Camera",
                    "httpVideoUrl": "https://video.example/cat/cam.m3u8",
                }
            ]),
            get_user_video_camera=AsyncMock(return_value={
                "id": "cam-extra-1",
                "name": "Extra Camera",
                "httpVideoUrl": "https://video.example/extra/cam.m3u8",
            }),
        )
        config_entry = SimpleNamespace(entry_id="entry-1", data={})
        areas_response = [
            {"folderId": "folder-123", "folderName": "Yard", "category": "House"},
            {"category": "Parking"},
            {"folderName": "Other", "cameraIds": ["cam-extra-1"]},
        ]
        entities = await camera._build_video_camera_entities(
            config_entry, api, None, None, areas_response
        )
        self.assertEqual(len(entities), 3)
        cam_names = [e._name for e in entities]
        self.assertIn("Folder Camera", cam_names)
        self.assertIn("Category Camera", cam_names)
        self.assertIn("Extra Camera", cam_names)
        api.get_folder_video_cameras.assert_awaited_once_with("folder-123")
        api.get_user_video_cameras.assert_awaited_once_with("Parking")
        api.get_user_video_camera.assert_awaited_once_with("cam-extra-1")


if __name__ == '__main__':
    unittest.main()
