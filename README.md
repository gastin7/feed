# feed

Автоматична генерація CSV-фіду товарів для OpenAI Ads (Ads Manager > Tools > Feeds) з Google-фіду Horoshop та публікація через GitHub Pages.

Щодоби (03:17 UTC) і за ручним запуском GitHub Actions завантажує XML-фід магазину, конвертує його скриптом `horoshop_to_openai_feed.py` у CSV за схемою OpenAI і публікує на GitHub Pages.

**Адреса фіду для OpenAI Ads:** https://gastin7.github.io/feed/openai_feed.csv

## Налаштування (один раз)

1. **Секрет з адресою фіду Horoshop.** Репозиторій > Settings > Secrets and variables > Actions > вкладка Secrets > New repository secret. Name: `FEED_URL`, Secret: повна адреса Google-фіду Horoshop (з токеном у шляху). Не Variable, а саме Secret. Адресу фіду не можна зберігати в коді, issue чи README: репозиторій публічний.
2. **GitHub Pages.** Settings > Pages > Build and deployment > Source: **GitHub Actions**.
3. (Необов'язково) Settings > Secrets and variables > Actions > вкладка Variables, змінні, якщо потрібні інші значення:
   - `SELLER_NAME`: назва продавця (за замовчуванням `DIAGNOST.NET.UA`);
   - `SELLER_URL`: сайт магазину (за замовчуванням `https://diagnost.net.ua`);
   - `EXCLUDE_REGEX`: регулярний вираз для виключення товарів за назвою (за замовчуванням `аналог|краш|програматор`).

## Ручний запуск

Вкладка Actions > workflow **openai-feed** > Run workflow > Run workflow. Після успішного завершення (зелена галочка) файл з'явиться за адресою вище.

## Параметри конвертації

Бренд береться лише з `g:brand` Horoshop-фіду: товар без бренду відхиляється (`no_brand`), жодної заглушки з назвою магазину немає. Скрипт запускається з опціями `--only-in-stock` (лише товари в наявності), `--force-https`, `--exclude-regex` (див. вище). Відхилені товари і причини дивіться в артефакті `rejected` у запуску workflow. Якщо після конвертації в CSV немає жодного товару, публікація зупиняється, а попередня версія файлу залишається доступною.

## Перевірка скрипта локально

```bash
python3 horoshop_to_openai_feed.py --self-test
```

## Примітки

- Планові workflow у публічних репозиторіях GitHub вимикає після ~60 днів без активності в репозиторії; за потреби вмикайте знову на вкладці Actions або робіть будь-який коміт.
- У репозиторії немає токенів і адреси фіду Horoshop, тільки скрипт і workflow.
