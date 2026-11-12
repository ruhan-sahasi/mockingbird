import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from mockingbird.app import create_app
from mockingbird.audio_store import AudioStoreError, LocalAudioStore, SupabaseAudioStore
from mockingbird.config import Settings
from mockingbird.store import MemoryStore

URL = "https://proj.supabase.test"
KEY = "sb-service-key-sentinel"
BUCKET = "recordings"


# --- LocalAudioStore --------------------------------------------------------------------------


async def test_local_round_trip_creates_parent_folders(tmp_path):
    store = LocalAudioStore(tmp_path)
    await store.put_wav("interviews/abc/candidate.wav", b"RIFFdata")
    assert (tmp_path / "interviews/abc/candidate.wav").read_bytes() == b"RIFFdata"


async def test_local_delete_prefix_removes_only_that_interviews_folder(tmp_path):
    store = LocalAudioStore(tmp_path)
    await store.put_wav("interviews/a/candidate.wav", b"1")
    await store.put_wav("interviews/a/interviewer.wav", b"2")
    await store.put_wav("interviews/b/candidate.wav", b"3")
    await store.delete_prefix("interviews/a")
    assert not (tmp_path / "interviews/a").exists()
    assert (tmp_path / "interviews/b/candidate.wav").read_bytes() == b"3"


async def test_local_delete_of_a_missing_prefix_is_fine(tmp_path):
    await LocalAudioStore(tmp_path).delete_prefix("interviews/none/")


async def test_local_delete_failure_surfaces(tmp_path, monkeypatch):
    store = LocalAudioStore(tmp_path)
    await store.put_wav("interviews/a/candidate.wav", b"x")

    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("mockingbird.audio_store.shutil.rmtree", boom)
    with pytest.raises(PermissionError):
        await store.delete_prefix("interviews/a")


@pytest.mark.parametrize("bad", ["../x", "/etc/x", "interviews/../../x"])
async def test_local_refuses_paths_that_escape_the_root(tmp_path, bad):
    store = LocalAudioStore(tmp_path / "root")
    with pytest.raises(ValueError):
        await store.put_wav(bad, b"x")
    with pytest.raises(ValueError):
        await store.delete_prefix(bad)


async def test_local_refuses_deleting_the_root_itself(tmp_path):
    store = LocalAudioStore(tmp_path)
    for bad in ["", ".", "interviews/.."]:
        with pytest.raises(ValueError):
            await store.delete_prefix(bad)


# --- SupabaseAudioStore -----------------------------------------------------------------------


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


def supabase(http):
    return SupabaseAudioStore(URL, KEY, BUCKET, http)


@respx.mock
async def test_supabase_put_wav_posts_the_object_with_upsert(http):
    route = respx.post(f"{URL}/storage/v1/object/{BUCKET}/interviews/a/candidate.wav").respond(200)
    await supabase(http).put_wav("interviews/a/candidate.wav", b"wavbytes")
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert req.headers["apikey"] == KEY
    assert req.headers["Content-Type"] == "audio/wav"
    assert req.headers["x-upsert"] == "true"
    assert req.content == b"wavbytes"


@respx.mock
async def test_supabase_delete_prefix_lists_then_deletes_with_one_slash(http):
    listing = respx.post(f"{URL}/storage/v1/object/list/{BUCKET}").respond(
        200, json=[{"name": "candidate.wav"}, {"name": "interviewer.wav"}]
    )
    delete = respx.delete(f"{URL}/storage/v1/object/{BUCKET}").respond(200, json=[])
    await supabase(http).delete_prefix("interviews/a")
    assert json.loads(listing.calls.last.request.content) == {
        "prefix": "interviews/a",
        "limit": 100,
    }
    sent = json.loads(delete.calls.last.request.content)
    assert sent == {"prefixes": ["interviews/a/candidate.wav", "interviews/a/interviewer.wav"]}
    for call in (listing.calls.last, delete.calls.last):
        assert call.request.headers["Authorization"] == f"Bearer {KEY}"
        assert call.request.headers["apikey"] == KEY


@respx.mock
async def test_supabase_delete_prefix_with_trailing_slash_does_not_double_it(http):
    respx.post(f"{URL}/storage/v1/object/list/{BUCKET}").respond(200, json=[{"name": "x.wav"}])
    delete = respx.delete(f"{URL}/storage/v1/object/{BUCKET}").respond(200, json=[])
    await supabase(http).delete_prefix("interviews/a/")
    assert json.loads(delete.calls.last.request.content) == {"prefixes": ["interviews/a/x.wav"]}


@respx.mock
async def test_supabase_delete_prefix_with_nothing_listed_sends_no_delete(http):
    respx.post(f"{URL}/storage/v1/object/list/{BUCKET}").respond(200, json=[])
    delete = respx.delete(f"{URL}/storage/v1/object/{BUCKET}").respond(200, json=[])
    await supabase(http).delete_prefix("interviews/a")
    assert delete.call_count == 0


@respx.mock
async def test_supabase_ensure_ready_creates_a_private_bucket(http):
    route = respx.post(f"{URL}/storage/v1/bucket").respond(200, json={"name": BUCKET})
    await supabase(http).ensure_ready()
    req = route.calls.last.request
    assert json.loads(req.content) == {"id": BUCKET, "name": BUCKET, "public": False}
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert req.headers["apikey"] == KEY


@respx.mock
async def test_supabase_ensure_ready_treats_a_bare_409_as_already_there(http):
    respx.post(f"{URL}/storage/v1/bucket").respond(409, json={"error": "Duplicate"})
    await supabase(http).ensure_ready()


@respx.mock
@pytest.mark.parametrize("code", ["409", 409])
async def test_supabase_ensure_ready_treats_400_with_a_409_body_as_already_there(http, code):
    # What Supabase Storage really sends: HTTP 400, the real code in the JSON body.
    respx.post(f"{URL}/storage/v1/bucket").respond(
        400,
        json={"statusCode": code, "error": "Duplicate", "message": "The resource already exists"},
    )
    await supabase(http).ensure_ready()


@respx.mock
@pytest.mark.parametrize(
    "body",
    [{"statusCode": "403", "error": "Unauthorized"}, {"error": "x"}, ["409"]],
)
async def test_supabase_ensure_ready_raises_on_a_400_with_another_body(http, body):
    respx.post(f"{URL}/storage/v1/bucket").respond(400, json=body)
    with pytest.raises(AudioStoreError) as err:
        await supabase(http).ensure_ready()
    assert err.value.status == 400


@respx.mock
async def test_supabase_ensure_ready_raises_on_a_400_that_is_not_json(http):
    respx.post(f"{URL}/storage/v1/bucket").respond(400, text="nope")
    with pytest.raises(AudioStoreError):
        await supabase(http).ensure_ready()


@respx.mock
async def test_supabase_error_holds_the_status_only(http):
    respx.post(f"{URL}/storage/v1/object/{BUCKET}/interviews/a/candidate.wav").respond(
        500, text=f"boom {KEY}"
    )
    with pytest.raises(AudioStoreError) as err:
        await supabase(http).put_wav("interviews/a/candidate.wav", b"x")
    assert err.value.status == 500
    assert "500" in str(err.value)
    for leaked in (KEY, "/storage/v1", URL, "boom"):
        assert leaked not in str(err.value)
        assert leaked not in repr(err.value)


@respx.mock
async def test_supabase_ensure_ready_raises_on_other_errors(http):
    respx.post(f"{URL}/storage/v1/bucket").respond(401)
    with pytest.raises(AudioStoreError):
        await supabase(http).ensure_ready()


# --- settings and app wiring ------------------------------------------------------------------


def test_new_settings_defaults(settings):
    assert settings.supabase_url is None
    assert settings.supabase_service_key is None
    assert settings.recordings_bucket == "recordings"
    assert settings.recordings_dir == "recordings"


def test_no_secret_appears_in_the_settings_repr():
    secrets = {
        "anthropic_api_key": "S-anthropic",
        "vapi_api_key": "S-vapi",
        "database_url": "postgresql://S-dburl",
        "brain_secret": "S-brain",
        "webhook_secret": "S-webhook",
        "owner_key": "S-owner",
        "visitor_salt": "S-salt",
        "supabase_service_key": "S-supabase",
    }
    s = Settings(_env_file=None, public_base_url="https://mockingbird.test", **secrets)
    text = repr(s) + str(s)
    for value in secrets.values():
        assert value not in text
    assert "https://mockingbird.test" in text


def test_without_supabase_settings_the_app_uses_a_local_folder(settings, tmp_path, caplog):
    local = settings.model_copy(update={"recordings_dir": str(tmp_path / "rec")})
    with TestClient(create_app(local, store=MemoryStore())) as c:
        store = c.app.state.audio_store
        assert isinstance(store, LocalAudioStore)
    assert caplog.text.count("recordings are kept in a local folder") == 1


@respx.mock
def test_with_both_supabase_settings_the_app_uses_supabase(settings):
    bucket = respx.post("https://proj.supabase.test/storage/v1/bucket").respond(200)
    cfg = settings.model_copy(update={"supabase_url": URL, "supabase_service_key": KEY})
    with TestClient(create_app(cfg, store=MemoryStore())) as c:
        store = c.app.state.audio_store
        assert isinstance(store, SupabaseAudioStore)
        http = store._http
        assert not http.is_closed
    assert http.is_closed
    assert bucket.call_count == 1


def test_one_supabase_setting_alone_falls_back_to_local(settings):
    cfg = settings.model_copy(update={"supabase_url": URL})
    with TestClient(create_app(cfg, store=MemoryStore())) as c:
        assert isinstance(c.app.state.audio_store, LocalAudioStore)


def test_an_injected_audio_store_is_used_and_made_ready(settings):
    class Fake:
        ready = 0

        async def ensure_ready(self):
            self.ready += 1

    fake = Fake()
    with TestClient(create_app(settings, store=MemoryStore(), audio_store=fake)) as c:
        assert c.app.state.audio_store is fake
    assert fake.ready == 1


def test_a_failing_ensure_ready_logs_the_type_and_the_app_still_starts(settings, caplog):
    class Broken:
        async def ensure_ready(self):
            raise AudioStoreError(503)

    with TestClient(create_app(settings, store=MemoryStore(), audio_store=Broken())) as c:
        assert c.get("/healthz").json() == {"ok": True}
    assert "AudioStoreError" in caplog.text
    assert "503" not in caplog.text
