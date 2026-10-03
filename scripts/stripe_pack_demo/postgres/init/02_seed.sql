-- Seed data, timestamps relative to container start so the 30-day first answer always
-- has data. :now_s is "now" in Unix seconds, a day is 86400 seconds. The README lists the
-- values every metric returns on this data.
SELECT extract(epoch FROM now())::bigint AS now_s \gset

-- Customers: four live, one test mode.
INSERT INTO stripe.customers (id, _account_id, created, currency, delinquent, email, livemode) VALUES
  ('cus_1',    'acct_demo', :now_s - 100*86400, 'usd', false, 'ada@example.com',   true),
  ('cus_2',    'acct_demo', :now_s -  60*86400, 'usd', false, 'grace@example.com', true),
  ('cus_3',    'acct_demo', :now_s -  20*86400, 'usd', true,  'alan@example.com',  true),
  ('cus_4',    'acct_demo', :now_s -   5*86400, 'eur', false, 'edsger@example.com',true),
  ('cus_test', 'acct_demo', :now_s -   3*86400, 'usd', false, 'test@example.com',  false);

-- Charges. Live USD succeeded charges are c1, c2, c3, c5, c6, c9. The others exist to show
-- what is excluded: a failed charge, a test mode charge and a EUR charge.
INSERT INTO stripe.charges
  (id, _account_id, amount, amount_captured, amount_refunded, captured, created, currency,
   customer, disputed, livemode, paid, refunded, status) VALUES
  ('ch_1', 'acct_demo', 10000, 10000,    0, true,  :now_s -  5*86400, 'usd', 'cus_1',    false, true,  true,  false, 'succeeded'),
  ('ch_2', 'acct_demo',  5000,  5000, 5000, true,  :now_s - 10*86400, 'usd', 'cus_2',    false, true,  true,  true,  'succeeded'),
  ('ch_3', 'acct_demo', 20000, 20000, 2500, true,  :now_s - 20*86400, 'usd', 'cus_3',    false, true,  true,  false, 'succeeded'),
  ('ch_4', 'acct_demo',  7500,     0,    0, false, :now_s -  3*86400, 'usd', 'cus_1',    false, true,  false, false, 'failed'),
  ('ch_5', 'acct_demo',  3000,  3000,    0, true,  :now_s - 45*86400, 'usd', 'cus_1',    false, true,  true,  false, 'succeeded'),
  ('ch_6', 'acct_demo',  4000,  4000, 4000, true,  :now_s - 40*86400, 'usd', 'cus_2',    false, true,  true,  true,  'succeeded'),
  ('ch_7', 'acct_demo',  9900,  9900,    0, true,  :now_s -  2*86400, 'usd', 'cus_test', false, false, true,  false, 'succeeded'),
  ('ch_8', 'acct_demo',  8000,  8000,    0, true,  :now_s -  7*86400, 'eur', 'cus_4',    false, true,  true,  false, 'succeeded'),
  ('ch_9', 'acct_demo', 12000, 12000,    0, true,  :now_s -  1*86400, 'usd', 'cus_3',    true,  true,  true,  false, 'succeeded');

-- Refunds. ch_6 was charged 40 days ago and refunded 2 days ago, so by refund date it falls
-- in the 30-day window while by charge date it does not. The last three rows are excluded:
-- a failed refund, a refund of the test mode charge and a EUR refund.
INSERT INTO stripe.refunds (id, _account_id, amount, charge, created, currency, reason, status) VALUES
  ('re_1', 'acct_demo', 5000, 'ch_2', :now_s -  8*86400, 'usd', 'requested_by_customer', 'succeeded'),
  ('re_2', 'acct_demo', 2500, 'ch_3', :now_s - 15*86400, 'usd', 'duplicate',             'succeeded'),
  ('re_3', 'acct_demo', 4000, 'ch_6', :now_s -  2*86400, 'usd', 'requested_by_customer', 'succeeded'),
  ('re_4', 'acct_demo', 1000, 'ch_1', :now_s -  4*86400, 'usd', NULL,                    'failed'),
  ('re_5', 'acct_demo', 9900, 'ch_7', :now_s -  1*86400, 'usd', NULL,                    'succeeded'),
  ('re_6', 'acct_demo', 2000, 'ch_8', :now_s -  3*86400, 'eur', NULL,                    'succeeded');

-- Subscriptions. items follows the Stripe API subscription object: a list whose items embed
-- their price (unit_amount in the smallest unit, recurring.interval and interval_count).
INSERT INTO stripe.subscriptions
  (id, _account_id, cancel_at_period_end, canceled_at, created, currency, customer, ended_at,
   items, livemode, start_date, status, trial_end) VALUES
  ('sub_1', 'acct_demo', false, NULL, :now_s - 90*86400, 'usd', 'cus_1', NULL,
   '{"object":"list","data":[{"id":"si_1","quantity":1,"price":{"id":"price_m20","unit_amount":2000,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   true, :now_s - 90*86400, 'active', NULL),
  ('sub_2', 'acct_demo', false, NULL, :now_s - 25*86400, 'usd', 'cus_2', NULL,
   '{"object":"list","data":[{"id":"si_2","quantity":1,"price":{"id":"price_y240","unit_amount":24000,"currency":"usd","recurring":{"interval":"year","interval_count":1}}}]}',
   true, :now_s - 25*86400, 'active', NULL),
  ('sub_3', 'acct_demo', false, NULL, :now_s -  5*86400, 'usd', 'cus_3', NULL,
   '{"object":"list","data":[{"id":"si_3","quantity":1,"price":{"id":"price_m50","unit_amount":5000,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   true, :now_s -  5*86400, 'trialing', :now_s + 9*86400),
  ('sub_4', 'acct_demo', false, :now_s - 10*86400, :now_s - 60*86400, 'usd', 'cus_1', :now_s - 10*86400,
   '{"object":"list","data":[{"id":"si_4","quantity":1,"price":{"id":"price_m20","unit_amount":2000,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   true, :now_s - 60*86400, 'canceled', NULL),
  ('sub_5', 'acct_demo', false, NULL, :now_s - 15*86400, 'usd', 'cus_2', NULL,
   '{"object":"list","data":[{"id":"si_5","quantity":3,"price":{"id":"price_m15","unit_amount":1500,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   true, :now_s - 15*86400, 'active', NULL),
  ('sub_6', 'acct_demo', false, NULL, :now_s -  4*86400, 'usd', 'cus_test', NULL,
   '{"object":"list","data":[{"id":"si_6","quantity":1,"price":{"id":"price_m99","unit_amount":9999,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   false, :now_s - 4*86400, 'active', NULL),
  ('sub_7', 'acct_demo', true, NULL, :now_s - 35*86400, 'usd', 'cus_3', NULL,
   '{"object":"list","data":[{"id":"si_7","quantity":1,"price":{"id":"price_m20","unit_amount":2000,"currency":"usd","recurring":{"interval":"month","interval_count":1}}}]}',
   true, :now_s - 35*86400, 'past_due', NULL);
