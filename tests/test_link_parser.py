from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.link_parser import (
    channel_internal_id_to_peer_id,
    find_telegram_message_reference,
    parse_telegram_reference,
)
from app.telegram_worker import TelegramPipeWorker, TelegramSourceError


class TelegramMessageReferenceParserTests(unittest.TestCase):
    def test_public_channel_message(self) -> None:
        reference = parse_telegram_reference("https://t.me/naraboxtvcom/242")

        self.assertEqual(reference.type, "public_channel_message")
        self.assertEqual(reference.username, "naraboxtvcom")
        self.assertEqual(reference.message_id, 242)
        self.assertIsNone(reference.topic_id)

    def test_private_channel_message(self) -> None:
        reference = parse_telegram_reference("https://t.me/c/2489865945/45515")

        self.assertEqual(reference.type, "private_channel_message")
        self.assertEqual(reference.channel_internal_id, 2489865945)
        self.assertEqual(reference.peer_id, -1002489865945)
        self.assertEqual(reference.message_id, 45515)

    def test_private_topic_uses_final_path_component_as_message(self) -> None:
        reference = parse_telegram_reference("https://t.me/c/2489865945/10/45517")

        self.assertEqual(reference.type, "private_channel_topic_message")
        self.assertEqual(reference.topic_id, 10)
        self.assertEqual(reference.message_id, 45517)

    def test_public_topic_message(self) -> None:
        reference = parse_telegram_reference("https://t.me/naraboxtvcom/10/45517")

        self.assertEqual(reference.type, "public_channel_topic_message")
        self.assertEqual(reference.topic_id, 10)
        self.assertEqual(reference.message_id, 45517)

    def test_query_parameters_trailing_slash_and_alternate_hosts(self) -> None:
        query_reference = parse_telegram_reference(
            "https://telegram.me/c/2489865945/45515/?single&thread=10#ignored"
        )
        public_reference = parse_telegram_reference("telegram.dog/naraboxtvcom/242?single")

        self.assertEqual(query_reference.topic_id, 10)
        self.assertEqual(query_reference.message_id, 45515)
        self.assertEqual(public_reference.username, "naraboxtvcom")

    def test_official_tg_deep_link_forms(self) -> None:
        private_reference = parse_telegram_reference(
            "tg://privatepost?channel=2489865945&post=45517&thread=10"
        )
        public_reference = parse_telegram_reference(
            "tg://resolve?domain=naraboxtvcom&post=242"
        )

        self.assertEqual(private_reference.peer_id, -1002489865945)
        self.assertEqual(private_reference.topic_id, 10)
        self.assertEqual(public_reference.username, "naraboxtvcom")

    def test_direct_private_channel_identifiers(self) -> None:
        from_internal = parse_telegram_reference(
            telegram_chat_id=2489865945,
            telegram_message_id=45515,
        )
        from_peer = parse_telegram_reference(
            telegram_chat_id=-1002489865945,
            telegram_message_id=45517,
            telegram_topic_id=10,
        )

        self.assertEqual(from_internal.peer_id, -1002489865945)
        self.assertEqual(from_peer.channel_internal_id, 2489865945)
        self.assertEqual(from_peer.canonical_url(), "https://t.me/c/2489865945/10/45517")

    def test_embedded_link_extraction(self) -> None:
        reference = find_telegram_message_reference(
            "New partner movie: https://t.me/c/2489865945/10/45517. Enjoy"
        )

        self.assertIsNotNone(reference)
        self.assertEqual(reference.message_id, 45517)

    def test_invalid_identifiers_are_rejected(self) -> None:
        invalid = [
            "https://t.me/c/not-a-number/45515",
            "https://t.me/c/2489865945/0",
            "https://t.me/naraboxtvcom/nope",
            "https://example.com/naraboxtvcom/242",
            "https://t.me/+invite-code",
        ]

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_telegram_reference(value)

    def test_peer_id_conversion(self) -> None:
        self.assertEqual(channel_internal_id_to_peer_id(2489865945), -1002489865945)


class TelegramReferenceResolutionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def worker_with_client(client: object) -> TelegramPipeWorker:
        worker = TelegramPipeWorker.__new__(TelegramPipeWorker)
        worker.client = client
        return worker

    async def test_private_channel_refreshes_dialogs_when_entity_cache_is_empty(self) -> None:
        entity = SimpleNamespace(id=2489865945, title="Partner Movies")

        async def dialogs():
            yield SimpleNamespace(entity=entity)

        client = SimpleNamespace(
            is_user_authorized=AsyncMock(return_value=True),
            get_entity=AsyncMock(side_effect=ValueError("cache miss")),
            iter_dialogs=dialogs,
        )
        worker = self.worker_with_client(client)
        reference = parse_telegram_reference("https://t.me/c/2489865945/45515")

        resolved = await worker._resolve_reference_entity(reference)

        self.assertIs(resolved, entity)
        client.get_entity.assert_awaited_once_with(-1002489865945)

    async def test_inaccessible_private_channel_has_specific_error(self) -> None:
        async def dialogs():
            if False:
                yield None

        client = SimpleNamespace(
            is_user_authorized=AsyncMock(return_value=True),
            get_entity=AsyncMock(side_effect=ValueError("cache miss")),
            iter_dialogs=dialogs,
        )
        worker = self.worker_with_client(client)
        reference = parse_telegram_reference("https://t.me/c/2489865945/45515")

        with self.assertRaisesRegex(TelegramSourceError, "not accessible.*-1002489865945"):
            await worker._resolve_reference_entity(reference)

    async def test_missing_message_has_specific_error(self) -> None:
        worker = self.worker_with_client(SimpleNamespace(get_messages=AsyncMock(return_value=None)))
        reference = parse_telegram_reference("https://t.me/c/2489865945/45515")

        with self.assertRaisesRegex(TelegramSourceError, "message 45515 does not exist or is inaccessible"):
            await worker._fetch_reference_message(reference, SimpleNamespace(id=2489865945))

    def test_video_sent_as_octet_stream_document_is_supported(self) -> None:
        worker = self.worker_with_client(SimpleNamespace())
        message = SimpleNamespace(
            media=object(),
            document=SimpleNamespace(attributes=[]),
            file=SimpleNamespace(name="Partner Movie.mkv", mime_type="application/octet-stream"),
        )

        self.assertTrue(worker._is_supported_media(message))

    def test_message_without_media_is_rejected(self) -> None:
        worker = self.worker_with_client(SimpleNamespace())
        self.assertFalse(worker._is_supported_media(SimpleNamespace(media=None)))


if __name__ == "__main__":
    unittest.main()
