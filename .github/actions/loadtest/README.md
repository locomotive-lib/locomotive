# CI Load Test Action

GitHub Action для запуска нагрузочного тестирования с использованием Locomotive.

## Использование

### Публичный репозиторий (проще всего)

Если репозиторий публичный, токены не нужны:

```yaml
- name: Set up Python
  uses: actions/setup-python@v7
  with:
    python-version: '3.13'

- name: Checkout locomotive
  uses: actions/checkout@v7
  with:
    repository: YOUR_ORG/locomotive
    path: locomotive
    ref: master  # или main, в зависимости от вашей default branch

- name: Run load test
  uses: ./locomotive/.github/actions/loadtest
  with:
    config: loconfig.json
    lib_repo: YOUR_ORG/locomotive
```

### Приватный репозиторий

Если репозиторий приватный, нужен токен `LOCOMOTIVE_TOKEN`:

```yaml
- name: Set up Python
  uses: actions/setup-python@v7
  with:
    python-version: '3.9'

- name: Checkout locomotive
  uses: actions/checkout@v7
  with:
    repository: YOUR_ORG/locomotive
    path: locomotive
    token: ${{ secrets.LOCOMOTIVE_TOKEN }}
    ref: master  # или main, в зависимости от вашей default branch

- name: Run load test
  uses: ./locomotive/.github/actions/loadtest
  with:
    config: loconfig.json
    lib_repo: YOUR_ORG/locomotive
    lib_token: ${{ secrets.LOCOMOTIVE_TOKEN }}
```

### Полный пример с параметрами

```yaml
- name: Run load test
  uses: ./locomotive/.github/actions/loadtest
  with:
    config: loconfig.json
    lib_repo: YOUR_ORG/locomotive
    lib_token: ${{ secrets.LOCOMOTIVE_TOKEN }}  # только для приватных репо
    users: 50
    run_time: 2m
    set_baseline: false
    baseline_artifact: loadtest-baseline
    results_artifact: loadtest-results
```

## Inputs

| Параметр | Описание | Обязательный | По умолчанию |
|----------|----------|--------------|--------------|
| `config` | Путь к конфигу loconfig.json | Нет | `loconfig.json` |
| `lib_repo` | Репозиторий библиотеки (owner/repo) | Да | - |
| `lib_token` | GitHub токен для доступа к приватному репо (только для приватных репо) | Нет | `github.token` |
| `users` | Количество пользователей (переопределяет конфиг) | Нет | - |
| `run_time` | Время выполнения теста (переопределяет конфиг) | Нет | - |
| `set_baseline` | Установить этот запуск как baseline | Нет | `false` |
| `baseline_artifact` | Имя артефакта для baseline | Нет | `loadtest-baseline` |
| `results_artifact` | Имя артефакта для результатов | Нет | `loadtest-results` |

## Outputs

| Параметр | Описание |
|----------|----------|
| `metrics_path` | Путь к файлу metrics.json |
| `report_path` | Путь к файлу report.html |
| `status` | Статус теста (PASS/WARNING/DEGRADATION) |

## Пример использования outputs

```yaml
- name: Run load test
  id: loadtest
  uses: ./locomotive/.github/actions/loadtest
  with:
    config: loconfig.json
    lib_repo: YOUR_ORG/locomotive
    lib_token: ${{ secrets.LOCOMOTIVE_TOKEN }}

- name: Check status
  run: |
    echo "Status: ${{ steps.loadtest.outputs.status }}"
    echo "Metrics: ${{ steps.loadtest.outputs.metrics_path }}"
    echo "Report: ${{ steps.loadtest.outputs.report_path }}"
```

## Что делает action

1. Устанавливает зависимости (locust, PyYAML)
2. Устанавливает Locomotive из указанного репозитория
3. Скачивает baseline артефакты (если есть)
4. Запускает нагрузочный тест
5. Загружает результаты в артефакты

## Требования

- Python 3.9+
- Конфиг `loconfig.json` в репозитории
- Для приватных репозиториев: секрет `LOCOMOTIVE_TOKEN` с PAT токеном (см. инструкции в основном README)