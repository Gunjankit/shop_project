# Shop: purchases, order history and a return-risk model

This is my take-home project. It is a small Django app for a shop that sells lamps and stools.
Part A records purchases. Part B imports old orders from a CSV file, shows them through an API,
and trains a small model that predicts whether an order will be returned.

Stack: Python 3.12, Django, Django REST framework, scikit-learn, SQLite.

## Python version

Python 3.12.7

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate          # creates the tables and seeds lamp, stool and cushion
```

The seed data (lamp with 2 in stock, stool with 1, and the cushion extra for the stool)
is loaded by a data migration, so `migrate` is the only setup command needed.
If you delete `db.sqlite3` and run `migrate` again, you get a fresh database with the same seed data.

## Run order

Part B needs the commands in this order:

```bash
python manage.py import_history order_history.csv   # 1. import the past orders
python manage.py train_return_model                 # 2. train and save the model
python manage.py runserver                          # 3. start the dev server
```

The model learns from the imported history, so the import has to come before the training.
If no model has been trained yet, the prediction endpoint answers 503.
`order_history.csv` is in the project root, exactly as it was given in the brief.

## Run the tests

```bash
pytest
```

`python manage.py test` should also work. The tests that need a trained model train it into a
temporary folder, so my real `models/` folder is never touched.

## Endpoints

| Method | URL | What it does |
|--------|-----|--------------|
| POST | `/api/purchases/` | Record a purchase |
| GET | `/api/catalog/<sku>` | Item details and remaining stock |
| GET | `/api/outbox` | Outbox messages in the order they were created |
| GET | `/api/history/` | Past orders, 10 per page. Filters: `sku`, `channel`, `from`, `to`, `page` |
| GET | `/api/history/stats` | Totals per sku. Params: `from`, `to` |
| POST | `/api/predictions/return-risk` | Probability that an order is returned |

There is also a small testing page at `/`. The brief did not ask for it. I added it so I could
try the purchase rules in the browser without typing curl commands. It only calls the same API.

## Project layout

```
config/                         Django settings and root urls
shop/models.py                  Item, Extra, Customer, Order, OutboxMessage, HistoricalOrder
shop/services.py                record_purchase (all purchase rules)
shop/views.py                   thin API views
shop/ml.py                      feature builder and cached model loader
shop/management/commands/       import_history, train_return_model
shop/migrations/                tables + seed data
shop/test_purchases.py          Part A tests
shop/test_history.py            Part B tests
order_history.csv               the data file from the brief
models/                         created by train_return_model (not in git)
```

## How I built it, step by step

I built it in small steps and made a git commit after each one, so the commit history follows this order.

### Step 0: Project setup
I created a virtual environment, installed Django, Django REST framework, scikit-learn, joblib
and pytest, started the Django project (`config`) and one app (`shop`), and registered
`rest_framework` and `shop` in `INSTALLED_APPS`. I also added a `.gitignore` for the venv,
the database and the trained model files.

### Step 1: Models
I created the five Part A models from the brief. Prices are stored as integer cents, to avoid
rounding problems with decimals. I added database-level safety nets next to the rules in the service:
- `Customer.email` is unique.
- `Order.external_charge_id` is unique.
- `(customer, item)` is unique on `Order`, which backs up the duplicate-purchase rule.
- `(item, code)` is unique on `Extra`.

`OutboxMessage` is ordered by id, so the messages come out in creation order.

### Step 2: Seed data
A data migration (`0002_seed_items`) creates lamp, stool and the stool-only cushion.
I used a migration so the data comes with `migrate` and there is no extra command to remember.

### Step 3: The `record_purchase` service
All purchase rules live in `shop/services.py`. The view does not contain any of them.

The function first trims and lowercases the email and checks the charge id format
(`^ch_[A-Za-z0-9]{8,}$`). After that, everything happens inside one `transaction.atomic()` block,
so either all writes are saved or none are. The rules run in the order from the brief:

1. **Replay:** if the charge id already belongs to an order, return that order with a `replayed`
   flag. Nothing else happens: no stock change, no outbox row, and the new sku, extra or email are ignored.
2. **Unknown item:** `unknown_item`.
3. **Extra:** a code the item does not sell gives `extra_not_allowed`. Because extras are linked to an item,
   the cushion is only found for the stool.
4. **Sold out:** `sold_out` when `remaining` is 0. This check runs before the customer is created,
   so a new email that hits a sold-out item leaves no customer row behind.
5. **Duplicate:** if this customer already has an order for this sku, `duplicate_purchase`.
6. **Success:** create the customer if needed, create the order (total = item price + extra price),
   decrease `remaining` by 1, then write the outbox rows. `account_created` is written first, but only
   if the customer was created in this call, then `purchase_recorded`.

I put the replay check first on purpose. If the stock check came first, a repeated request for
the last stool would wrongly return `sold_out` instead of the original order.
The item row is read with `select_for_update()` so two buyers of the last item cannot both succeed
on a database that supports row locks.

### Step 4: The purchase, catalog and outbox API
The views only do three things: read the JSON body, call the service, and turn the result into an HTTP response.

| Outcome | HTTP |
|---------|------|
| Created | 201, `{ "replayed": false, "order": {...} }` |
| Replay | 200, `{ "replayed": true, "order": {...} }` |
| `sold_out`, `duplicate_purchase` | 409 |
| `unknown_item`, `extra_not_allowed`, `invalid_charge_id`, missing fields | 400 |

The JSON uses camelCase (`externalChargeId`, `totalCents`) as in the brief, and the code underneath uses snake_case.

### Testing page (extra)
A single HTML page at `/` with a purchase form, the current stock and the outbox.
It uses `fetch` against the API, so it contains no business rules.

### Step 5: Part A tests
The tests use Django's `TestCase`, so the database is reset between tests. A small `snapshot()`
helper records stock, customer count, order count and outbox count. Failure tests compare the
snapshot before and after, which is how I check that a failed purchase changes nothing.
They cover:
- a sold-out item with a brand-new email (no customer is saved);
- a duplicate customer and sku;
- an extra the item does not sell;
- a replay of the same charge id with a different sku in the body;
- a new email creating one customer and an `account_created` message, with the extra included in the total;
- every failure type leaving stock, customers, orders and outbox unchanged;
- a crash in the middle of a purchase (I force the outbox write to raise an error) to prove that the
  transaction really rolls everything back;
- the HTTP status codes and the catalog and outbox endpoints.

### Step 6: Importing the history
I added a `HistoricalOrder` model. It is separate from `Order`, so importing old data never touches live stock.
It has indexes on `(created_at, order_id)` and `(email, created_at)`.

`import_history` checks each row in the order the brief gives, and the first failed check is the rejection reason:
`duplicate_order_id`, `invalid_email`, `unknown_sku`, `extra_not_allowed`, `bad_total`, `bad_date`,
`bad_returned`, `bad_channel`.

Details I paid attention to:
- Values are trimmed and lowercased before checking (so `"  Ada@Example.COM "` and `" Lamp"` are fine).
- Dates must be ISO 8601 with a timezone. A date like `2026-06-31` has the right shape but is not a real
  date, so I check the shape first and then parse it. Valid dates are stored in UTC.
- For `returned` I test `is None`, not `not returned`, because `False` is a valid answer.
- The duplicate check is against earlier lines in the same file. A valid row whose `order_id` is already in the
  database is **skipped**, not rejected.
- Rows are written with `bulk_create` in batches of 500. For every batch I find the already-existing ids
  with one query, so there is no query per row.
- The last line printed is the JSON summary: `created`, `skippedExisting` and `rejected`
  (only the reasons that happened). Running the command twice leaves the table exactly as after the first run.

For the provided file the result is 112 created and 10 rejected rows
(2 duplicate ids, 2 invalid emails, 1 unknown sku, 1 extra not allowed, 1 bad total, 2 bad dates, 1 bad returned value).
I checked these numbers against the real output of the command, not only by counting by hand.

### Step 7: History list and stats endpoints
- `GET /api/history/` returns 10 rows per page, ordered by `createdAt` and then `orderId`.
  The tie-break on `orderId` keeps the order stable when two orders have the same time.
- `from` and `to` are compared with the UTC date and both ends are included. I do this with a range:
  `created_at >= from 00:00 UTC` and `created_at < (to + 1 day) 00:00 UTC`.
  This includes the whole last day and still lets the database use its index.
- `GET /api/history/stats` uses ORM aggregation (`values("sku").annotate(Count, Sum, filtered Count)`),
  so the database does the counting and summing in one query. Both skus are always in the answer;
  if a sku has no orders in the window, the result is `orders: 0`, `revenueCents: 0`, `returnRate: null`.
  `returnRate` is rounded to 2 decimals.

### Step 8: Training the return model
`train_return_model` builds the six features from the brief:
`is_stool`, `has_extra`, `channel_app`, `channel_web`, `hour` (UTC) and `prior_orders`.
`prior_orders` is the number of earlier orders by the same email, strictly before that order's time.
I get it in one pass by sorting each email's order times and using `bisect_left`.

Then it splits the data 80/20 with `train_test_split(test_size=0.2, random_state=0, stratify=y)`,
fits `LogisticRegression(max_iter=1000, random_state=0)` on the 80%, and saves:
- `models/return_model.joblib`: the fitted model
- `models/metrics.json`: `version`, `rows`, `trainRows`, `holdoutAccuracy`, `baselineAccuracy`, `features`

`baselineAccuracy` is the accuracy of always guessing the most common class on the holdout set.
The `version` is a timestamp plus a short random suffix, so it changes each time I train.

The feature code lives in one function, `build_feature_row` in `shop/ml.py`. Both training and the API use it.
That way the two cannot drift apart.

### Step 9: Prediction endpoint
`POST /api/predictions/return-risk` checks whether a model exists first and answers
503 `model_not_trained` if not. Then it validates the input (an unknown sku gives 400 `unknown_item`),
counts the prior orders for that email before `createdAt` from `HistoricalOrder`, builds the features,
and returns `{ "probability": ..., "modelVersion": ... }` with the probability rounded to 3 places.

The model is loaded once and kept in memory. On each request I only compare the file modification times,
and the model is reloaded only when the files changed. So it is not loaded on every request,
and a newly trained model is picked up without restarting the server.

### Step 10: Part B tests
- Importing `order_history.csv` gives 112 created and the exact per-reason rejected counts,
  and a second import creates nothing and leaves the table unchanged.
- Cleaned rows look right (lowercase emails, trimmed sku, `true` and `no` become booleans), and
  the live stock is not changed by the import.
- History: pagination, ordering, filters, inclusive date window, bad parameters.
- Stats for May 2026 are compared with numbers I worked out separately from the CSV, and an empty window
  returns zeros and `null`.
- Prediction: 503 before training; after training (done in the test setup) 200 with a probability between
  0 and 1 and a model version. Retraining gives a new version that the API picks up.

## Choices the brief did not specify

- Missing or wrong-typed fields in the purchase body: 400 `missing_fields`.
- Unknown sku on `/api/catalog/<sku>`: 404 `unknown_item`.
- Bad `from` or `to` on the history endpoints: 400 `invalid_date`. Bad `page`: 400 `invalid_page`.
- Prediction input errors: 400 `invalid_created_at`, `invalid_channel`, `extra_not_allowed`.
- `account_created` is only written when the customer is created in that call, not for returning customers.
- Pages beyond the last one return an empty `results` list with the correct `count`.

## Time spent

About 6 hours of actual work, spread over 3 days. Started 13 November 2026, finished 15 November 2026.

## What is missing

Nothing

## What I'd improve

If the history file had 10 million rows:
- The `seen_ids` set used for duplicate detection holds every id in memory. I would replace it with a
  staging table or a sort-based duplicate check.
- `train_return_model` loads all rows into Python. I would compute `prior_orders` with an SQL window
  function, train an incremental model (for example `SGDClassifier` with `partial_fit`), and run training
  as a background job.
- `OFFSET` pagination gets slow on late pages. I would use keyset pagination on `(created_at, order_id)`.
- I would move to PostgreSQL, keep the indexes, and think about partitioning the history table by month.

Other things:
- **Concurrency:** `select_for_update` only really locks on PostgreSQL. I would add a threaded test there
  and catch `IntegrityError` on a duplicate charge id, so two identical simultaneous requests resolve as a replay.
- **Model:** the accuracy was not tuned (out of scope). I would add proper evaluation, model versioning and monitoring.
- **Validation:** I would use DRF serializers for request validation and put the duplicated email and date
  rules from the import command and the API into one shared module.
- The testing page and the API have no authentication, which a real shop would need.

## AI tools

- **Tools used:** Claude.
- **Used for:** explaining the brief to me step by step, drafting the service, the import command, the model
  command, the tests and this README. I ran every step myself, read the code and the output, and checked
  the import and stats numbers against the real command output.
