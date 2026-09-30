from mirea_lecture_assistant.chat_detection import (
    attendance_like_messages,
    classmates_report_attendance_issue,
)

GROUP = "ИКБО-01-24"
OWN = "Иванов Иван ИКБО-01-24"


def test_detects_two_distinct_classmate_messages():
    page = """
    Чат
    10:41 Петров Пётр ИКБО-01-24
    QR не работает
    Сидорова Анна ИКБО-01-24
    """
    assert classmates_report_attendance_issue(page, GROUP, OWN)


def test_groups_do_not_have_to_match_ours():
    page = """
    Петров Пётр БСБО-11-23
    Смирнова Анна КВБО-07-25
    """
    assert classmates_report_attendance_issue(page, GROUP, OWN)


def test_ignores_own_duplicate_and_unrelated_chat():
    page = f"""
    {OWN}
    {OWN}
    Петров Пётр ИКБО-02-24
    Иванов Иван, отметьте меня пожалуйста
    """
    assert attendance_like_messages(page, GROUP, OWN) == ("Петров Пётр ИКБО-02-24",)
    assert not classmates_report_attendance_issue(page, GROUP, OWN)


def test_one_matching_message_is_not_enough():
    assert not classmates_report_attendance_issue("Петров Пётр ИКБО-01-24", GROUP, OWN)
