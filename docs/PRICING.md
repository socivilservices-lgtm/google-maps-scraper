# Pricing

Checked 14 November 2026. Prices below are what those vendors publish for Google Maps lead data. They are not quotes from this product.

## What comparable tools charge

| Tool | Published price | What the quota buys | Source |
| --- | --- | --- | --- |
| Outscraper Google Maps Scraper | First 500 businesses each 30 days free, then $3 per 1,000 up to 100,000, then $1 per 1,000. No monthly subscription. Emails are a separate product with the same shape. | Pay only for rows. | [outscraper.com/pricing](https://outscraper.com/pricing/), also summarized on 9 Nov 2026 by [Search Legion](https://searchlegion.com/reviews/outscraper) and [Leadtrapper](https://useleadtrapper.com/articles/outscraper-alternative/) |
| Apify `compass/crawler-google-places` | Place scraped: $4 / 1,000 on the free plan, $3 on Starter ($19/month), $2 on Scale ($199), $1.50 on Business ($999). Contacts, filters, and reviews are extra events. | Platform plan plus per-place events. A Starter customer pulling 3,000 places pays about $19 + $9 before add-ons. | [Actor pricing](https://apify.com/compass/crawler-google-places/pricing), [Leadraft, Nov 2026](https://leadraft.co/blog/apify-pricing-explained) |
| PhantomBuster Google Maps Search Export | Start is $69/month, or $56/month billed annually, for 20 hours of execution and 5 slots. A 14-day trial includes 2 hours. Google Maps is described as about 120 results per search. Emails need a second Phantom. | You pay for time, not rows, so cost per lead moves around. | [PhantomBuster comparison on Scrap.io](https://scrap.io/alternative-phantombuster), [Leads Sniper vs PhantomBuster](https://www.leads-sniper.com/blog/leads-sniper-vs-phantombuster) |
| Scrap.io | Basic $49/month ($35/month yearly) for 10,000 export credits. Professional $99 / 20,000, Agency $199 / 40,000, Company $499 / 100,000. A credit is one business. Seven-day trial capped at 100 leads. | Hosted directory plus export. Emails are included. | [scrap.io/pricing](https://scrap.io/pricing) as quoted by [OutboundXYZ, 16 Oct 2026](https://www.outboundxyz.com/blog/google-maps-lead-scrapers) and [Leads Sniper, July 2026](https://www.leads-sniper.com/blog/scrap-io-vs-leads-sniper) |
| Leads Sniper | $149 once for one license. Advertised as unlimited, with about 120 results per Google Maps search because it drives the browser. | A desktop extension, not a hosted multi-user app. | [leads-sniper.com](https://www.leads-sniper.com/blog/leads-sniper-vs-phantombuster) |

Usage-priced APIs (Outscraper, Apify) are cheaper per thousand rows once you already have somewhere to run them. Hosted subscriptions (Scrap.io, PhantomBuster) start at $49–$69 a month.

## What a result costs us

Google bills Places API (New) **per request**, at the highest SKU any field in the field mask touches. From the [core services price list](https://developers.google.com/maps/billing-and-pricing/pricing) (page current as of this check; SKU text last updated 11 Nov 2026 on the [Places SKU page](https://developers.google.com/maps/billing-and-pricing/sku-details/places)):

| SKU | Free requests / month | Price per 1,000 after that, up to 100,000 |
| --- | --- | --- |
| Text Search Pro | 5,000 | $32 |
| Text Search Enterprise (phone, website, hours, rating) | 1,000 | $35 |
| Text Search Enterprise + Atmosphere (also reviews) | 1,000 | $40 |
| Place Details Enterprise | 1,000 | $20 |
| Place Details Enterprise + Atmosphere | 1,000 | $25 |

One Text Search returns up to 20 places. Three thousand results are about 150 Text Search requests. At the Enterprise + Atmosphere rate that is **$6** after the free 1,000 requests, and **$0** while the project is still inside the free cap.

A follow-up Place Details call per business is a different story: 3,000 detail calls at $25 / 1,000 is **$75**, which is more than the subscription. The Playwright backend does not pay Google per request. It spends CPU and memory instead, and it stops when Google shows a CAPTCHA. A small DigitalOcean app with 1–2 GB of RAM is on the order of **$12–$25 / month**. Stripe takes 2.9% + $0.30, about **$1.14** on a $29 charge.

## Recommendation

**Pro is $29 per month.**

Each paid month, and each 14-day invite trial, includes:

- **3,000 business results**
- **100 searches**
- **100 results per search**

The admin account (`ADMIN_EMAIL`) is not counted against this quota.

Why this, and not a cheaper unlimited plan:

- $29 is below Scrap.io Basic ($49) and PhantomBuster Start ($56–$69). A buyer who wants a hosted login, API key, and CSV/JSON without learning Apify can start here.
- $29 / 3,000 results is $9.67 per thousand. That is higher than Outscraper's $3 and Apify Starter's $3 per thousand, on purpose. Those products meter raw rows. This price includes the app, accounts, and a fixed bill.
- At full quota on Playwright, cost is roughly hosting ($12–$25) plus Stripe ($1). Margin stays positive on one active subscriber.
- At full quota on Places Text Search only, add about $0–$6 of Google fees. Still positive.
- At full quota with a Place Details call for every business, Google's bill can pass $70 and the plan loses money. Production should prefer Playwright, or keep Text Search on the Enterprise field mask and avoid per-place detail calls, unless the Google project is still inside its free monthly cap.

The quota is enforced in the app for every account that is not the admin. Trials get the same numbers for 14 days, which is what "full features" means here: the same search, the same fields, and the same monthly cap, not an unlimited firehose.

Raise the price or cut the quota before turning on per-place Place Details for every search. The constants live in `python/src/gmaps_service/saas/billing.py` and can be overridden with `GMAPS_MONTHLY_RESULT_QUOTA`, `GMAPS_MONTHLY_SEARCH_QUOTA`, and `GMAPS_MAX_RESULTS_PER_SEARCH`.
