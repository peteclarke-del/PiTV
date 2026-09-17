from pitv.scheduler.policy import DAY, HOUR, SchedulerPolicy


def policy(**settings):
    return SchedulerPolicy(settings, now=1_000_000)


def test_channel_values_inherit_and_override_global_rules():
    rules = policy(short_episode_minutes=20, short_episode_run_minutes=25,
                   band_item_max_minutes=15, band_item_repeat_hours=36,
                   band_feature_repeat_days=14)
    inherited = {"short_episode_minutes": None, "short_episode_run_minutes": None,
                 "band_item_max_minutes": None, "band_item_repeat_hours": None,
                 "band_feature_repeat_days": None}
    assert rules.short_episode_seconds(inherited) == (20 * 60, 25 * 60)
    assert rules.band_limits(inherited) == (15, 36 * HOUR, 14 * DAY)

    overridden = {**inherited, "short_episode_minutes": 12, "band_item_repeat_hours": 6}
    assert rules.short_episode_seconds(overridden) == (12 * 60, 25 * 60)
    assert rules.band_limits(overridden) == (15, 6 * HOUR, 14 * DAY)


def test_remote_lead_and_episode_cadence_are_named_policy_rules():
    rules = policy(external_lead_hours=23, external_weight=1.5,
                   series_cadence_days=7, series_cadence_bonus=4.0)
    assert not rules.external_prepared(rules.now + 23 * HOUR)
    assert rules.external_prepared(rules.now + 23 * HOUR + 1)
    assert rules.external_weight(rules.now + HOUR) == 0.1
    assert rules.external_weight(rules.now + DAY) == 1.5

    last = rules.now
    assert not rules.next_episode_due(last, last + 6 * DAY)
    assert rules.next_episode_due(last, last + 7 * DAY - 12 * HOUR)
    assert rules.cadence_factor(last, last + 7 * DAY) == 4.0


def test_policy_uses_authoritative_defaults_when_a_setting_is_absent():
    rules = policy()
    assert rules.advert_break_seconds == 4 * 60
    assert rules.short_episode_seconds({}) == (20 * 60, 20 * 60)
