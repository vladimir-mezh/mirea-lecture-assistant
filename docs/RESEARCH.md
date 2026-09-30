# Исследование интеграций MIREA

Проверено 3 сентября 2026 года по исходному коду `silverhans/pymirea`, PyPI и открытому
API расписания. Это снимок состояния внешних сервисов; они могут измениться без
предупреждения.

## Вывод

`pymirea` пригодна как основная интеграция с MIREA, но должна находиться за адаптером:
API ещё молодой, опубликованный релиз отстаёт от ветки `main`, а сетевые ответы MIREA
частично разбираются эвристически. Приложение использует только подтверждённые методы и
не реализует собственную отправку посещаемости.

## Ответы на вопросы спецификации

1. **Версия.** На PyPI опубликована `0.3.3` (29 апреля 2026), тогда как в `main` на
   момент проверки указана `0.4.0`. В проекте принят диапазон `>=0.3.3,<0.5` и собственный
   `MireaService` как граница совместимости.

2. **SSO.** `MireaAuth.login(username, password)` открывает Keycloak authorization
   endpoint realm `mirea`, client `attendance-app`, применяет PKCE S256, отправляет форму
   и меняет authorization code на токены. При OTP возвращается `AuthResult.challenge`,
   продолжение выполняет `complete_2fa(challenge, code)`. Сессию можно проверить через
   `verify_session(cookies)`.

3. **Расписание.** Для авторизованного студента используется
   `MireaGrades(session_cookies).get_schedule(days=N)`. Это личное расписание из Pulse,
   а не поиск произвольной группы. Для независимого поиска группы возможен fallback к
   открытому API `schedule-of.mirea.ru` (поиск объекта, затем iCal/API), но он не нужен
   для первого авторизованного сценария.

4. **Поля занятия.** Фактически `ScheduleLesson` содержит `name`, `lesson_type`, `room`,
   `teacher`, `start_epoch`, `end_epoch`, `subgroup`. Стабильных `id`, `source_url` и
   отдельного `is_online` в модели нет; локальный ID приходится формировать из времени и
   названия, а URL хранить собственной привязкой.

5. **URL онлайн-занятия.** Модель расписания `pymirea` URL не возвращает. Нужен
   `LectureLinkResolver`: сначала URL из источника (если появится), затем сохранённая
   привязка предмета, после этого явный запрос пользователю.

6. **QR.** `MireaAPI.extract_token_from_qr(qr_data)` принимает UUID либо URL с одним из
   разрешённых MIREA-доменов и возвращает `(token, error)`. В локальной истории нельзя
   сохранять сам token; приложение сохраняет SHA-256 отпечаток.

7. **Отправка.** `MireaAPI.mark_attendance(qr_data)` сначала вызывает официальный
   gRPC-Web flow `SelfApproveAttendanceThroughQRCode` через
   `MireaGrades.self_approve_attendance(token)`, а при неопределённом результате имеет
   legacy fallback на `/selfapprove?token=…`. Результат — `AttendanceResult(success,
   message, user_name)`.

8. **Ошибки.** Старые методы преимущественно возвращают result-объекты, `None` или
   `False`, а сетевые участки могут пробрасывать исключения `httpx`. Новые helper-методы
   вводят `MireaError`, `MireaSessionExpired`, `MireaRefreshFailed`, `MireaRateLimited`,
   `MireaServerError`, `MireaParseFailed`. Адаптер не должен заставлять UI зависеть от
   этих конкретных типов.

9. **Fallback расписания.** Для личного расписания после входа достаточно Pulse +
   локального кэша. Fallback к `schedule-of.mirea.ru` полезен для первого запуска без
   входа и поиска произвольной группы, но требует отдельной модели доверия и обработки
   iCal. Его разумно добавить после стабилизации v0.1.

10. **Повторное использование.** Наиболее непосредственно применим `pymirea`. Из
    открытых проектов полезны `Wyndace/rtu-mirea-schedule-api` как документированный
    пример чтения `schedule-of.mirea.ru` и `serguun42/mss` как более ранняя система
    расписаний. Код неизвестных «scanner» проектов не следует переносить без проверки
    лицензии, актуальности endpoint-ов и безопасности хранения сессий.

11. **Чат MTS Link.** Официальная документация подтверждает пользовательский сценарий:
    открыть кнопку «Чат», ввести текст в поле «Введите сообщение» и нажать Enter.
    Публичный REST endpoint `POST /v3/eventsessions/{eventsessionID}/chat` требует
    `x-auth-token` и ID сессии мероприятия, которых у локального помощника нет. Поэтому
    адаптер использует только подписанную браузерную сессию и доступные названия элементов;
    при изменении интерфейса он возвращает явную ошибку и не кликает произвольные поля.

## Источники

- [pymirea на GitHub](https://github.com/silverhans/pymirea)
- [pymirea на PyPI](https://pypi.org/project/pymirea/)
- [RTU MIREA Schedule API](https://github.com/Wyndace/rtu-mirea-schedule-api)
- [MIREA Schedule System](https://github.com/serguun42/mss)
- [Чат и вопросы MTS Link](https://help.mts-link.ru/article/19732)
- [API отправки сообщения MTS Link](https://help.mts-link.ru/article/19626)
