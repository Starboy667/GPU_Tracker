import pytest

from pricewatch import forms, netsafe
from pricewatch.models import Channel, ProductURL, Settings, Watch
from pricewatch.notify import CHANNEL_TYPES
from pricewatch.scrapers import SCRAPERS, validate_site_url

pytestmark = pytest.mark.usefixtures("public_dns")


def parse(form, **kw):
    args = dict(site_keys=SCRAPERS, channel_ids=[1, 2, 3], validate_site_url=validate_site_url)
    return forms.parse_watch(form, **{**args, **kw})


BASE = {"name": "RTX 5070", "site_ldlc": "on", "enabled": "on"}


@pytest.mark.parametrize("raw,expected", [
    ("1079.95", 1079.95), ("1 079,95 €", 1079.95), ("1" + chr(0xA0) + "079,95", 1079.95), ("", None), ("  ", None),
])
def test_parse_number(raw, expected):
    assert forms.parse_number(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "1.2.3", "nan", "inf"])
def test_parse_number_rejects_garbage(raw):
    with pytest.raises(ValueError):
        forms.parse_number(raw)


def test_minimal_watch_gets_sensible_defaults():
    watch, errors = parse(BASE)
    assert errors == {}
    assert (watch.name, watch.query, watch.sites, watch.enabled) == ("RTX 5070", "RTX 5070", ("ldlc",), True)
    assert watch.match == r"RTX\s*5070(?![\s-]*(?:ti|xt|xtx|super|gre)\b)"
    assert watch.in_stock_only is None and watch.channel_ids is None and watch.product_urls == ()
    assert watch.min_price is None and watch.max_price is None and watch.deal_drop_pct is None


def test_full_watch():
    watch, errors = parse({
        **BASE, "query": "geforce 5070", "match": "5070", "exclude": "PC|laptop", "min_price": "150",
        "max_price": "599,90", "deal_drop_pct": "5", "stock_rule": "any", "notify_restock": "on",
        "site_idealo": "on", "url_ldlc": " https://www.ldlc.com/informatique/cat.html ",
        "url_idealo": "https://www.idealo.fr/cat/1.html", "url_grosbill": "https://evil.example/",  # unticked: ignored
        "product_urls": "https://shop.example/tv\n\nhttps://www.amazon.fr/dp/B0 | .a-price .a-offscreen\nhttps://shop.example/tv",
        "channels_mode": "selected", "channel_2": "on", "channel_9": "on", "enabled": "",
    })
    assert errors == {}
    assert (watch.query, watch.match, watch.exclude) == ("geforce 5070", "5070", "PC|laptop")
    assert (watch.min_price, watch.max_price, watch.deal_drop_pct) == (150, 599.9, 5)
    assert watch.in_stock_only is False and watch.notify_restock and not watch.enabled
    assert watch.sites == ("ldlc", "idealo")
    assert dict(watch.site_urls) == {
        "ldlc": "https://www.ldlc.com/informatique/cat.html", "idealo": "https://www.idealo.fr/cat/1.html"}
    assert watch.product_urls == (
        ProductURL("https://shop.example/tv"), ProductURL("https://www.amazon.fr/dp/B0", ".a-price .a-offscreen"))
    assert watch.channel_ids == (2,)  # unknown channel 9 dropped


def test_watch_errors_are_reported_per_field():
    watch, errors = parse({
        "name": "", "match": "(", "exclude": "[", "min_price": "abc", "max_price": "-5", "deal_drop_pct": "200",
        "stock_rule": "maybe", "site_ldlc": "on", "url_ldlc": "https://evil-ldlc.com/x", "product_urls": "ftp://x/y",
    })
    assert watch is None
    assert set(errors) == {"name", "match", "exclude", "min_price", "max_price", "deal_drop_pct", "stock_rule",
                           "url_ldlc", "product_urls"}
    assert "ldlc.com" in errors["url_ldlc"] and errors["product_urls"].startswith("Line 1:")


def test_watch_needs_somewhere_to_look():
    assert "sites" in parse({"name": "TV"})[1]
    assert parse({"name": "TV", "product_urls": "https://shop.example/tv"})[1] == {}


def test_watch_target_below_minimum_is_refused():
    assert "max_price" in parse({**BASE, "min_price": "500", "max_price": "100"})[1]


def test_watch_length_limits():
    assert "name" in parse({**BASE, "name": "x" * 101})[1]
    assert "query" in parse({**BASE, "query": "x" * 201})[1]
    assert "match" in parse({**BASE, "match": "x" * 501})[1]
    too_many = "\n".join(f"https://shop.example/{i}" for i in range(21))
    assert "At most" in parse({**BASE, "product_urls": too_many})[1]["product_urls"]
    assert "too long" in parse({**BASE, "product_urls": "https://shop.example/" + "x" * 2000})[1]["product_urls"]


def test_private_product_page_is_refused():
    assert "private" in parse({**BASE, "product_urls": "http://127.0.0.1:8080/admin"})[1]["product_urls"]


def test_editing_keeps_identity_and_history_fields():
    existing = Watch(id=7, name="Old", query="old", match="old", created_at="2026-01-01T00:00:00Z",
                     last_checked_at="2026-11-25T09:00:00Z")
    watch, _ = parse(BASE, existing=existing)
    assert (watch.id, watch.created_at, watch.last_checked_at, watch.name) == (
        7, "2026-01-01T00:00:00Z", "2026-11-25T09:00:00Z", "RTX 5070")


def test_product_urls_round_trip():
    pages = (ProductURL("https://a.example/x"), ProductURL("https://b.example/y", ".price"))
    assert forms.parse_product_urls(forms.format_product_urls(pages)) == (pages, None)


# ------------------------------------------------------------------ settings

GOOD_SETTINGS = {
    "deal_drop_pct": "7,5", "check_interval_minutes": "45", "baseline_days": "21", "baseline_min_days": "4",
    "renotify_drop_pct": "3", "dealabs_max_age_days": "2", "health_alert_after": "0", "history_days": "90",
}


def test_settings_parse():
    settings, errors = forms.parse_settings({**GOOD_SETTINGS, "in_stock_only": "on"})
    assert errors == {}
    assert settings == Settings(deal_drop_pct=7.5, in_stock_only=True, check_interval_minutes=45, baseline_days=21,
                                baseline_min_days=4, renotify_drop_pct=3.0, dealabs_max_age_days=2,
                                health_alert_after=0, history_days=90)
    assert forms.parse_settings(GOOD_SETTINGS)[0].in_stock_only is False


@pytest.mark.parametrize("field,value", [
    ("check_interval_minutes", "5"), ("check_interval_minutes", "30.5"), ("deal_drop_pct", "0"),
    ("baseline_days", ""), ("history_days", "abc"), ("baseline_min_days", "30"),
])
def test_settings_errors(field, value):
    settings, errors = forms.parse_settings({**GOOD_SETTINGS, field: value})
    assert settings is None and field in errors


# ------------------------------------------------------------------ channels

NTFY = CHANNEL_TYPES["ntfy"]


def test_channel_parse_and_defaults():
    channel, errors = forms.parse_channel({"cfg_topic": "my-deals", "event_deal": "on", "enabled": "on"}, NTFY)
    assert errors == {}
    assert (channel.name, channel.type, channel.events, channel.enabled) == ("ntfy", "ntfy", ("deal",), True)
    assert channel.config["topic"] == "my-deals" and channel.config["server_url"] == "https://ntfy.sh"


def test_channel_errors():
    channel, errors = forms.parse_channel({"name": "x" * 101, "cfg_server_url": "http://10.0.0.5"}, NTFY)
    assert channel is None
    assert set(errors) == {"name", "events", "cfg_topic", "cfg_server_url"}
    assert errors["cfg_topic"] == "Required."
    ok, errors = forms.parse_channel(
        {"cfg_topic": "t", "cfg_server_url": "http://10.0.0.5", "event_deal": "on"}, NTFY, allow_private=True)
    assert errors == {} and not ok.enabled


def test_channel_edit_keeps_blank_secrets_and_identity():
    existing = Channel(id=4, name="Phone", type="ntfy", created_at="2026-01-01T00:00:00Z",
                       config={"server_url": "https://ntfy.sh", "topic": "old", "token": "tk_secret", "priority": "3"})
    channel, errors = forms.parse_channel(
        {"name": "Phone 2", "cfg_topic": "new", "cfg_token": "", "event_deal": "on", "event_health": "on"},
        NTFY, existing=existing)
    assert errors == {}
    assert (channel.id, channel.name, channel.created_at) == (4, "Phone 2", "2026-01-01T00:00:00Z")
    assert channel.config["token"] == "tk_secret" and channel.config["topic"] == "new"
    assert channel.events == ("deal", "health")

    unreadable = Channel(id=4, name="Phone", type="ntfy", config={}, readable=False)
    fixed, errors = forms.parse_channel({"cfg_topic": "t", "event_deal": "on"}, NTFY, existing=unreadable)
    assert errors == {} and fixed.readable


# ------------------------------------------------------------------ accounts

def test_new_user():
    assert forms.parse_new_user({"email": " Bob@Example.org ", "password": "long enough pw", "is_admin": "on"}) == (
        ("bob@example.org", "long enough pw", True), {})
    parsed, errors = forms.parse_new_user({"email": "nope", "password": "short"})
    assert parsed is None and set(errors) == {"email", "password"}


def test_password_change():
    good = {"current_password": "old password!", "new_password": "new password!", "confirm_password": "new password!"}
    assert forms.parse_password_change(good) == (("old password!", "new password!"), {})
    assert "current_password" in forms.parse_password_change({**good, "current_password": ""})[1]
    assert "new_password" in forms.parse_password_change({**good, "new_password": "short"})[1]
    assert "confirm_password" in forms.parse_password_change({**good, "confirm_password": "different one"})[1]
    same = {"current_password": "same password", "new_password": "same password", "confirm_password": "same password"}
    assert "new_password" in forms.parse_password_change(same)[1]


def test_netsafe_error_type_is_what_forms_catch():
    with pytest.raises(netsafe.UnsafeURLError):
        validate_site_url("ldlc", "https://example.org/")
