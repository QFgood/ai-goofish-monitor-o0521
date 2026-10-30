from src.core.cron_utils import build_cron_trigger, validate_cron_expression


def test_validate_cron_expression_normalizes_alias():
    assert validate_cron_expression("@daily") == "0 0 * * *"


def test_validate_cron_expression_accepts_six_fields():
    assert validate_cron_expression("0 0 8 * * *") == "0 0 8 * * *"


def test_build_cron_trigger_accepts_alias_and_timezone():
    trigger = build_cron_trigger("@hourly", timezone="Asia/Shanghai")

    assert trigger is not None
    assert str(trigger.timezone) == "Asia/Shanghai"


def test_validate_cron_expression_rejects_invalid_value():
    try:
        validate_cron_expression("not-a-cron")
    except ValueError as exc:
        assert "支持 5 段" in str(exc)
        return

    raise AssertionError("非法 cron 应该抛出 ValueError")


def test_named_weekend_from_time_picker_fires_only_saturday_and_sunday():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone=ZoneInfo('Asia/Shanghai')
    trigger=build_cron_trigger('5 12 * * sat,sun',timezone=zone)
    saturday=trigger.get_next_fire_time(None,datetime(2026,10,30,13,0,tzinfo=zone))
    assert (saturday.weekday(),saturday.hour,saturday.minute)==(5,12,5)
    sunday=trigger.get_next_fire_time(saturday,saturday)
    assert sunday.weekday()==6
    following=trigger.get_next_fire_time(sunday,sunday)
    assert following.weekday()==5
