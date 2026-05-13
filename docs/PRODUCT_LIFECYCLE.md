# Product Lifecycle — Per-Line Walkthrough

**Status:** Implemented — VP product spawn + per-Line walkthrough ship in v0.0.1.
**Companions:** [`docs/ORG_MODEL.md`](./ORG_MODEL.md) (org structure)
· [`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) (engineering).
**Audience:** Product / strategy thinking + Line Pack authors.

`ORG_MODEL.md` answers *who works for whom*. This doc answers *what
each unit does, in what order, with what tools.* It's the process
model that complements the org model.

If you read only one section, read the **Build vs Integrate
decision framework** at the bottom — it's the single highest-leverage
guideline for whether an agent should call a 3rd-party SaaS or
spawn a custom-built mini-app.

---

## The Six Phases

Every product moves through the same six phases — what differs by Line
is *which agent owns each phase*, *what 3rd-party SaaS gets integrated*,
and *which skills the playbook ships*.

| # | Phase | Output |
|---|-------|--------|
| 1 | **Create** | A product exists (digital file, manuscript, design, SaaS app, sourced supplier, service definition) |
| 2 | **Copywriting** | All copy + marketing materials ready (sales page, emails, social bank, ad variants, listing copy, FAQ, support templates) |
| 3 | **Order-readiness** | Buyers can actually order — web pages / funnels / payment flow live, OR platform listing optimized and approved |
| 4 | **Eyeball-getting** | Traffic flows. Five sub-streams: 4a organic social · 4b GEO/AEO · 4c SEO · 4d platform listings as channel · 4e paid ads |
| 5 | **Delivery** | Buyer receives the product (digital download, POD shipment, dropship, ecom-with-inventory, SaaS access, agency deliverable) |
| 6 | **Customer support** | Buyer is handled post-purchase, with variable autonomy from full-auto FAQ to human-only escalation |

## Summary Matrix — Lines × Phases

Quick reference. Each cell is a one-liner; deep walkthroughs follow.

|  | **Create** | **Copy** | **Order-Ready** | **Eyeball** | **Deliver** | **Support** |
|---|------------|----------|-----------------|-------------|-------------|-------------|
| **POD** | Design files via z-image-turbo / human + AI iteration | Listing titles, tags, lifestyle ad copy | Etsy / Merch / Printful listing optimization | Pinterest + Etsy SEO + Amazon Ads | Printful / Printify fulfills | Etsy handles 80%, light agent assist |
| **KDP** | Manuscript via Sudowrite / Claude + human edit; cover via z-image-turbo + designer | Book description, author bio, A+ content, ad copy, ARC outreach | KDP listing (7 keywords, 2 categories, A+ content) | Amazon Ads + BookBub + author newsletter + GEO/AEO | Amazon prints + ships paperback / delivers Kindle / ACX audio | FAQ on Author Central, ARC team handles reviews |
| **Info Products** | Course recorded via Loom + Riverside; ebook in Vellum / Atticus | Sales page, email sequence (welcome → tripwire → core → OTOs), webinar script, JV swipes | Funnel: Teachable / Kajabi / ThriveCart / Stripe; webinar via EverWebinar / WebinarJam | Organic social + JV launches + paid ads on Meta / YouTube + AEO for "how do I" queries | Course platform delivers; bonuses via email | Helpdesk (HelpScout / Crisp); community on Discord / Circle |
| **SaaS** | Codex CLI / Claude Code dev cycles → deploy via Vercel / Fly / Cloudflare | Landing + onboarding emails + in-app copy + docs + changelog | Sign-up flow + Stripe subscription + onboarding wizard | SEO + content marketing + Product Hunt + paid ads + AEO | Continuous delivery via GitHub Actions | Intercom / Crisp + auto-FAQ; agent has Level-2 autonomy on refunds |
| **Affiliate** | No product creation. Build *audiences* (list segments) — ConvertKit / Beehiiv | Swipe emails per launch, bonus stack copy, social hooks, webinar pitch | JV page (like the one we just built); affiliate platform setup (JVZoo / W+) | Owned email list (the asset) + reciprocal mailings + bonus stacks | Vendor delivers; affiliate hands buyers off | Vendor handles support; affiliate fields "did I get my bonus?" questions only |
| **Agency** | Define service tiers + SOPs (Notion / ClickUp); no goods | Scope-of-work template, proposal generator, onboarding doc, delivery reports | Booking via Calendly / SavvyCal + Stripe / ThriveCart payment; contract via PandaDoc / Bonsai | LinkedIn outreach + content + referrals + paid Google Ads on intent | Deliverable management via ClickUp / Asana / Notion; reports via Loom | Front / Missive for client comms; weekly status updates auto-generated |

---

## Line 1: Print on Demand (POD)

POD is **design-led + niche-narrow + platform-multiplexed.** A POD shop
makes its money by publishing many designs across many products
(t-shirts, mugs, stickers, posters, phone cases) on multiple platforms
(Printful for Shopify, Merch by Amazon, Redbubble, Society6, TeePublic,
Etsy) and letting print-on-demand partners handle fulfillment.

### Phase 1 — Create

**What:** Generate design files (PNG with transparent background for tees,
SVG for cuttable, high-DPI for posters/mugs). Test ideas at low cost,
ship the winners.

**Who:** POD Line VP → Type Manager (T-Shirts / Mugs / Stickers) →
optional Niche Manager (Cat lovers / Software engineers / …) → Designer
agent.

**3rd-party integrations:** Midjourney, DALL-E, Ideogram (paid AI image
gen with licensing). Adobe Express / Canva for layout/text. Background
removal via remove.bg or **z-image-turbo from Vidyo's GPU mesh** (shared
resource, free at marginal cost).

**Korpha skills:** `image.generate(model="z-image-turbo", style="vector")` ·
`image.remove_background()` · `pod.iterate_design(niche, base_concept,
n_variants=10)` · `pod.validate_against_platform(file, platform="merch_by_amazon")`.

**Build-vs-integrate:** Image generation = **integrate** with shared
resource (Vidyo mesh). Design iteration loop = **build** (skill that
sequences generation + critique + revision).

### Phase 2 — Copywriting

**What:** Listing titles (Etsy's 140-char limit), 13 Etsy tags (each
≤20 chars), Merch by Amazon's 2-line "brand+title" copy, Pinterest pin
descriptions, ad creative captions.

**Who:** POD Type Manager → Copywriter agent.

**3rd-party:** None native to copy. Some authors use eRank for keyword
research on Etsy (built-in skill can replicate).

**Korpha skills:** `pod.write_etsy_listing(design, niche)` ·
`pod.write_merch_amazon_2liner(design)` · `pod.write_pinterest_pin(design, audience)`.

### Phase 3 — Order-Readiness

**What:** Listing optimization across platforms. No own-domain funnel
typically — POD lives on marketplaces.

**Who:** POD Niche Manager publishes to platforms; agent handles APIs.

**3rd-party:** Printful API (Shopify integration), Printify API (Etsy
integration), Merch by Amazon (manual upload + API for accepted accounts),
Redbubble (manual upload + API), Society6 / TeePublic / Spreadshirt.

**Korpha skills:** `pod.publish_to_printful(design, products[])` ·
`pod.publish_to_merch_amazon(design, tier)` · `pod.publish_to_etsy(design, shop_id, listing)`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** Pinterest is #1 for POD (visual + buyer intent).
Instagram + TikTok for viral. **Skills:** `social.post_to_pinterest`,
`social.post_to_instagram`, `social.repurpose_to_tiktok`. Tools:
Tailwind for Pinterest scheduling, Buffer for cross-posting.

**4b GEO/AEO:** Low priority for POD (buyers query Amazon/Etsy, not
ChatGPT). Skip unless niche-specific tutorials drive traffic.

**4c SEO:** Pinterest pins act as SEO inside Pinterest. Etsy SEO is its
own game (handled in 4d).

**4d Platform listings as channel:** This is *the* channel for POD.
Etsy SEO (title front-loaded with keywords, 13 tags, 10 photos),
Merch by Amazon BSR optimization, Pinterest pin-as-listing strategy.
**Skills:** `pod.optimize_etsy_listing_for_search`, `pod.merch_by_amazon_bsr_strategy`.

**4e Paid ads:** Pinterest Ads (cheapest for POD), Etsy Ads (built-in
auction), Amazon Sponsored Products (essential for Merch by Amazon).
Facebook/Instagram Ads for branded shops. **Skills:**
`ads.pinterest_create_campaign`, `ads.etsy_promoted_listings`,
`ads.amazon_sponsored_products(asin, daily_budget)`.

### Phase 5 — Delivery

**What:** POD partner prints + ships. The founder never touches inventory.

**3rd-party:** Printful, Printify, Merch by Amazon, Redbubble — handles
end-to-end. Tracking number flows back via webhook.

**Korpha skills:** `pod.track_shipment(order_id)` ·
`pod.notify_customer_shipped(order_id)`. Webhook handlers ingest events
from Printful/Printify into the kanban as `CardArtifact` rows.

### Phase 6 — Customer Support

**What:** Etsy / Merch by Amazon handle 80% (defective product, "where
is my order"). Founder fields rare custom-request questions only.

**Autonomy level:** Level 3 (full autonomy on FAQ + order lookup).

**Korpha skills:** `support.lookup_order_status(order_id)` ·
`support.respond_etsy_message(thread_id, draft)`. Integration: Etsy
Messages API, Merch by Amazon's seller messaging.

### POD walkthrough

> Andrew opens the POD Line VP. "Want a new Cat Lovers Niche under
> T-Shirts." → POD Line VP spawns the Niche Manager + queues 10
> kanban cards:
>
> 1. Niche research (uses `pod.research_niche("cat lovers", platform="etsy")`)
> 2-6. Five design concepts via z-image-turbo
> 7. Bg removal + format conversion
> 8. Listing copy via `pod.write_etsy_listing` × 5
> 9. Publish to Etsy + Printful + Merch by Amazon
> 10. Pinterest pin batch (5 designs × 3 pins each = 15 pins)
>
> Three days later, two designs are pulling sales. POD Niche Manager
> proposes via cross-line cooperation: *"these two designs are
> converting at 4.2%. Want me to repurpose them into a Substack post
> for the SaaS line's audience or commission a TikTok for the Info
> line?"* — cooperation goes through the phone-call API; the
> receiving Line VPs decide independently.

---

## Line 2: Amazon KDP

KDP is **genre-fragmented + Amazon-dominated + audience-by-pen-name.** Each
genre (Romance, Coloring, Cookbook, Business Non-fiction, Children's,
Comics) has its own playbook. Romance demands series + tropes + pen
names. Coloring is batch-published, design-heavy, Amazon-Ads-driven.
Cookbooks need recipe testing + photos + USDA compliance.

### Phase 1 — Create

**What:** Manuscript + cover + (optional) audiobook narration.

**Who:** KDP Line VP → Type Manager (Romance / Coloring / Cookbook) →
optional Series Lead → Author agent + Cover Designer agent.

**3rd-party integrations:** Sudowrite (fiction-specific AI writer),
Claude / GPT-4 for prose, **Atticus** (cross-platform formatter),
**Vellum** (Mac-only, gold standard). For covers: **Book Brush**,
Canva, Midjourney + Photoshop. Audiobook: **ACX** (Audible), **Findaway
Voices**, or **OmniVoice voice clone from Vidyo's GPU mesh** (shared
resource, free for narration drafts).

**Korpha skills:** `kdp.outline_book(genre, premise, length)` ·
`kdp.draft_chapter(outline, chapter_n, voice_brief)` ·
`kdp.generate_cover_concepts(book_metadata, n=5)` ·
`audio.synthesize(text, voice="omnivoice:cloned-author")` for audiobook
drafts.

### Phase 2 — Copywriting

**What:** Book description (the most critical conversion asset on KDP),
author bio, A+ content text, Amazon Ads copy, ARC outreach email, newsletter swipes.

**Who:** KDP Type Manager → Copywriter agent specialized for the genre.

**3rd-party:** **Publisher Rocket** for keyword research (some authors
swear by it). KDSpy. Helium 10 (multi-purpose Amazon tool).

**Korpha skills:** `kdp.write_book_description(book, hook_style)` ·
`kdp.write_aplus_content(book)` · `kdp.find_keywords(genre, comp_titles)`.

### Phase 3 — Order-Readiness

**What:** KDP listing setup. 7 keyword slots, 2 category slots,
title/subtitle, A+ content (Brand Registry required), pricing
strategy. Plus paperback POD-via-Amazon listing (separate setup).

**Who:** KDP Type Manager → Listing optimization specialist agent.

**3rd-party:** **KDP itself** (Author Central), **Author Central**
for author page, **KDP Direct** API (limited; mostly manual).

**Korpha skills:** `kdp.publish_listing(book, keywords, categories,
description, price)` · `kdp.set_kdp_select_or_wide(book, mode)` ·
`kdp.upload_aplus_content(book, blocks)`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** Author-specific. TikTok (BookTok) is #1 for
fiction. Instagram (bookstagram) for fiction + cookbooks. Newsletter is
the most stable channel (own list, no algorithm). **Skills:**
`social.post_to_booktok(book)`, `social.post_to_bookstagram(book)`.

**4b GEO/AEO:** Growing. ChatGPT now recommends books in response to "I
need a book about X." Optimizing for AEO citation is the new Amazon SEO.
Use **RankMyAnswer Line Pack** (Andrew's other product — installs as a
Type Pack under KDP for AEO playbook).

**4c SEO:** Author website + book landing page for evergreen Google
traffic. Lower priority than AEO + Amazon Ads.

**4d Platform listings as channel:** Amazon's internal search is THE
channel. 7 keyword optimization, category strategy, BSR maintenance,
review volume. **Skills:** `kdp.research_amazon_keywords(genre)` ·
`kdp.monitor_bsr(book)` · `kdp.suggest_category_swap(book, current, alt[])`.

**4e Paid ads:** **Amazon Ads** is essential (Sponsored Products,
Sponsored Brands, Lockscreen Ads). Facebook Ads for fiction. **BookBub
Ads** for promo days. **Skills:** `ads.amazon_kdp_sponsored_products`,
`ads.facebook_book_promo`, `ads.bookbub_create_campaign`.

### Phase 5 — Delivery

**What:** Amazon prints (paperback POD), Kindle Direct delivers ebook,
ACX delivers audiobook. The founder ships nothing physical.

**3rd-party:** All Amazon-managed. ACX for Audible delivery.

**Korpha skills:** None needed for delivery itself. Monitor sales
via `kdp.fetch_sales_dashboard(book)` for the dashboard.

### Phase 6 — Customer Support

**What:** Amazon handles ALL customer-facing support (returns, refunds,
defective products). Author handles:
- Reader email (newsletter subscribers asking about next book)
- Negative review responses (carefully — never argue)
- ARC team questions

**Autonomy level:** Level 1-2 (draft replies for human approval —
negative reviews are reputation-critical).

**Korpha skills:** `support.draft_reply_to_reader(email, voice)` ·
`support.flag_negative_review_for_human(review)`.

### KDP walkthrough

> KDP Romance Type Manager has shipped 4 books in the "Highland Rogue"
> series. Series Lead spawns kanban for book #5:
>
> 1. Outline (uses series bible + last book's reader feedback from
>    audience-scoped memory recall — namespace-isolated, no leak to
>    other Type Mgrs)
> 2. Draft chapters (Sudowrite + Claude alternating; founder approves
>    every 3rd chapter)
> 3. Cover concepts via z-image-turbo (style locked to series)
> 4. Audiobook draft via OmniVoice clone (uses Andrew's voice clone
>    stored as shared resource, only available in local install mode)
> 5. Book description via `kdp.write_book_description(book, "Highland-saga voice")`
> 6. Listing keywords via `kdp.research_amazon_keywords("highlander romance")`
> 7. Publish via `kdp.publish_listing` (one API call, handles all 7
>    keyword slots + categories)
> 8. Amazon Ads campaign launch ($20/day initial budget, scales on RoAS)
> 9. ARC team email blast (200 reviewers via ConvertKit segment)
> 10. BookTok post via `social.post_to_booktok(book, hook_variant)`
>
> Two months later, book #5 is at BSR 3,200 in Highland Historical
> Romance. AEO scoring via the installed RMA Line Pack shows the
> series cited 3× in Perplexity for "best Highland romance series."

---

## Line 3: Info Products

Info products are **funnel-driven + audience-segmented + JV-launch-amplified.**
The economics are sharp: front-end $37–67 with 4 OTOs leveraging cold
traffic, recurring upsells (membership / community), and JV launches
that multiply EPC through affiliate networks.

### Phase 1 — Create

**What:** Course modules (video + audio + workbook), ebook, newsletter
issues, membership content, DFY templates.

**Who:** Info Line VP → Type Manager (Courses / Ebooks / Newsletter /
Membership / DFY) → Course Creator agent or Content agent.

**3rd-party integrations:** Recording: **Loom**, **Riverside**, **OBS
Studio**, **Descript** (for editing transcripts). Slide decks: Google
Slides, Pitch, Tome. Course hosting: **Teachable**, **Thinkific**,
**Podia**, **Kajabi**, **MemberSpace**, **MemberStack**. Newsletter:
**Substack**, **Beehiiv**, **ConvertKit landing pages**.

**Korpha skills:** `info.draft_module_outline(course, n_modules)` ·
`info.draft_workbook(module)` · `info.transcribe_recording(audio_url)`
(uses **Whisper from Vidyo mesh** — shared resource).

### Phase 2 — Copywriting

**What:** This is where info products live or die. Sales letter
(long-form 3-5K words), email sequences (welcome, tripwire, launch,
post-launch), webinar pitch script, JV affiliate swipes + bonus copy.

**Who:** Info Type Manager → Copywriter agent (long-form specialist).

**3rd-party:** **AI copy tools** (Copy.ai, Jasper) but we're an AI
cofounder — we ARE the tool. **Funnel template libraries** like
ClickFunnels Vault as reference.

**Korpha skills:** `info.write_sales_letter(offer, audience,
length)` · `info.write_email_sequence(launch, days[])` ·
`info.write_jv_bonus_stack(launch)`.

### Phase 3 — Order-Readiness

**What:** Funnel build. FE sales page → checkout → OTO1 → OTO2 → OTO3 →
thank-you. Plus webinar registration page if launch is webinar-driven.

**Who:** Info Line VP → Funnel Architect agent.

**3rd-party:** Funnel builders: **ClickFunnels**, **ThriveCart**,
**SamCart**, **Kartra**. Checkout: **Stripe Checkout**, **Stripe
Payment Links**, **Lemon Squeezy** (Merchant of Record — handles VAT).
Webinar: **WebinarJam**, **EverWebinar**, **Zoom Webinars**.

**Korpha skills:** `funnel.build_fe_oto_ladder(offer, prices[],
copy_blocks)` · `commerce.create_payment_link(amount, name)` (existing) ·
`funnel.setup_webinar_registration_page(webinar, ESP)`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** YouTube long-form is the gold standard for info
products (search intent + evergreen). Podcasts. X/Twitter threads as
content hooks. **Skills:** `social.publish_youtube_video(video,
metadata)`, `social.post_x_thread(content)`.

**4b GEO/AEO:** Critical for info products. ChatGPT/Claude users ask
"how do I X" and "best course for Y" — being cited in those responses
is the new top-of-funnel. **RankMyAnswer Line Pack** is the playbook.

**4c SEO:** Long-form blog content + YouTube SEO + Pinterest for
lifestyle/coaching niches. Lower priority than YouTube + AEO.

**4d Platform listings as channel:** **Udemy** for high-volume
low-margin distribution. **Skool** / **Mighty Networks** for
community-driven info. Marketplace listings supplement (don't replace)
own-funnel sales.

**4e Paid ads:** **Meta** (Facebook/Instagram) is #1 for info.
YouTube Ads (TrueView) excellent for course pre-roll. Pinterest Ads for
lifestyle. **JV launches** amplify everything — affiliates promote
the launch, you get cold traffic at zero ad spend.

### Phase 5 — Delivery

**What:** Course platform delivers (Teachable/Kajabi). Email-delivery
bonuses via ConvertKit/Beehiiv. Community access via Skool/Discord/Circle.

**3rd-party:** All handled by the chosen course platform + ESP +
community tool.

**Korpha skills:** `info.grant_course_access(buyer_email, course_id,
platform)` · `info.send_bonus_email(buyer, bonus_id)`.

### Phase 6 — Customer Support

**What:** Two distinct support flows.
1. Pre-purchase questions ("does this work for X?") — short, sales-adjacent.
2. Post-purchase ("how do I do module 3?") — community + helpdesk.

**Autonomy level:** Level 2 for pre-sale (auto-reply with confidence
threshold). Level 3 for post-sale FAQ. Level 1 for refund requests.

**3rd-party:** **Crisp**, **HelpScout**, **Intercom** for ticketing.
**Skool** / **Circle** / **Discord** for community engagement.

**Korpha skills:** `support.answer_presale_question(question, offer)` ·
`support.community_engagement(channel, recent_posts)`.

### Info walkthrough

> Info Line VP's Course Type Manager spawns "AI Cofounder Mastery"
> course. JV launch planned for 60 days out.
>
> 1. Curriculum outline (12 modules, ~6 hours total video)
> 2. Slide decks per module (Google Slides via API)
> 3. Record screen-share via Loom (founder-led)
> 4. Transcripts via Whisper (Vidyo mesh)
> 5. Workbooks per module
> 6. Sales letter (5K words, written + iterated 3 times)
> 7. Email sequence: 7 days warm-up + launch week + 7 days post-launch
> 8. Webinar registration page + webinar script
> 9. JV page (mirror RankMyAnswer / Korpha JV page structure)
> 10. Bonus stack: 3 DFY templates + 1 mini-course + community access
> 11. Funnel: FE $47 → OTO1 $97 (advanced) → OTO2 $197 (DFY) → OTO3 $497 (cohort)
> 12. Cart-open day: webinar pitch → cart open → affiliates promote
>
> The Affiliate Line VP (separate line) sees the launch on the JV
> calendar. Compatibility check against its 3 audiences via
> `niche.score_fit` — AI marketers audience scores 0.92 (perfect
> match). Affiliate Line VP proposes via phone-call API to Info Line
> VP: *"I'll mail my AI marketers list 3×; in return, 30% rev share
> + 2 reciprocal mails on my next own-launch."* Info Line VP accepts.

---

## Line 4: SaaS Apps

SaaS is **recurring-revenue + dev-cycle-driven + technical-support-heavy.**
Economics depend on MRR growth minus churn. Unlike POD/KDP, the product
keeps evolving forever — there's no "ship and done."

### Phase 1 — Create

**What:** Build the app. Backend, frontend, database, deploys.

**Who:** SaaS Line VP → Product VP per app → Dev team agents (Codex CLI
delegation is heavy here).

**3rd-party integrations:** **Codex CLI** / **Claude Code** /
**OpenCode** for development (shared OAuth CLI resources). **GitHub**
for source. **Vercel** / **Fly.io** / **Cloudflare Pages** / **Railway**
for hosting. **Supabase** / **Neon** / **Render** for databases.
**Sentry** for error tracking.

**Korpha skills:** `dev.codex_run(task, repo)` (existing #155) ·
`dev.deploy_to_vercel(repo, env)` · `dev.deploy_to_cloudflare_pages(repo)`.

### Phase 2 — Copywriting

**What:** Landing page, onboarding emails, in-app copy (empty states,
error messages, tooltips), docs, changelog, sales emails.

**Who:** SaaS Product VP → Copywriter agent (B2B SaaS voice).

**3rd-party:** Docs sites: **Mintlify**, **GitBook**, **Notion**.
Landing builders: **Framer**, **Webflow** if you want CMS; otherwise
just static deploy.

**Korpha skills:** `saas.write_landing_page(product, audience)` ·
`saas.write_onboarding_emails(product, n_days)` ·
`saas.write_in_app_copy(screen, intent)`.

### Phase 3 — Order-Readiness

**What:** Sign-up flow, Stripe subscription setup, onboarding wizard,
billing portal.

**Who:** SaaS Product VP → Dev agent.

**3rd-party:** **Stripe Billing** (subscriptions, metered billing, trial
periods, dunning). **Paddle** if you want Merchant of Record. **Outseta**
for all-in-one (CRM + billing + helpdesk). **LemonSqueezy** for solo
SaaS with simpler tax.

**Korpha skills:** `commerce.create_stripe_subscription_product(name,
prices[])` · `saas.build_signup_flow(auth_provider, email_verification)` ·
`saas.build_onboarding_wizard(steps[])`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** **X/Twitter** is #1 for SaaS founders. LinkedIn
for B2B. YouTube long-form for technical content. Podcasts. Substack for
thought leadership.

**4b GEO/AEO:** **HUGE for SaaS.** People ask AI "what's the best SaaS
for X" constantly. Use RankMyAnswer Line Pack. SaaS that ranks in
ChatGPT for its category captures massive top-of-funnel.

**4c SEO:** Programmatic SEO is the meta for SaaS (comparison pages,
alternative-to pages, integration-with pages). Tools: **Ahrefs**,
**SEMrush**, **Frase**, **SurferSEO**.

**4d Platform listings as channel:** **Product Hunt** for launch.
**AlternativeTo**, **G2**, **Capterra**, **GetApp** for B2B.
**Indie Hackers**, **r/SaaS**, **Hacker News** Show HN.

**4e Paid ads:** **Google Search Ads** for high-intent keywords. **Meta
Ads** for consumer SaaS. **LinkedIn Ads** for B2B (expensive but
targeted). **Reddit Ads** for niche communities.

### Phase 5 — Delivery

**What:** Continuous delivery. Code merged → CI runs → auto-deploys.
Users get updates seamlessly.

**3rd-party:** **GitHub Actions**, **Vercel** auto-deploy on push,
**Fly.io** CLI deploys, **Cloudflare Pages** Git integration.

**Korpha skills:** `dev.create_github_action_pipeline(repo, config)` ·
`dev.monitor_deploy_status(deploy_id)`.

### Phase 6 — Customer Support

**What:** Two flows.
1. Bug reports → engineering kanban.
2. How-do-I questions → docs / FAQ / agent reply.

**Autonomy level:** Level 3-4 (full autonomy on FAQ; agent can reproduce
bugs in sandbox, propose fixes; refunds under $50 auto-approved).

**3rd-party:** **Intercom** for in-app chat, **Crisp** for cheaper,
**HelpScout** for email-first, **Linear** / **GitHub Issues** for bug
tracking, **Sentry** for error context.

**Korpha skills:** `support.intercom_auto_reply(thread, kb)` ·
`support.reproduce_bug_in_sandbox(error_id)` (uses Codex CLI) ·
`support.propose_fix_pr(bug_id)`.

### SaaS walkthrough

> Korpha as a SaaS product itself. SaaS Line VP → Korpha Product VP:
>
> - Continuous: Codex CLI runs background jobs (#155) implementing
>   features, fixing bugs, generating docs.
> - Weekly: Product VP reviews kanban, prioritizes next sprint, drafts
>   changelog.
> - Monthly: P&L review shows Korpha MRR vs other lines. CEO sees
>   the roll-up; Korpha Product VP sees its own slice.
> - Daily: Intercom auto-replies handle 60% of support; rest escalate
>   to Andrew with one-paragraph summaries.

---

## Line 5: Affiliate Marketing

Affiliate is **audience-segmented + campaign-time-bounded + reciprocity-driven.**
You don't create products — you promote *other people's* products to
your *audience segments*. The asset is the list; campaigns are
time-bounded windows where you fire at specific launches.

### Phase 1 — Create

**What:** Build *audiences* (list segments). This is the only Line where
"creation" doesn't produce a product — it builds the asset that all
future campaigns leverage.

**Who:** Affiliate Line VP → Audience Manager per niche (AI marketers /
solopreneur productivity / KDP authors).

**3rd-party integrations:** **ConvertKit**, **Beehiiv**, **MailerLite**,
**GetResponse**, **AWeber** for ESP. **Substack**, **Beehiiv newsletter
mode** for hybrid newsletter+list. **Sender**, **Buttondown** for solo.
Lead magnets via **ConvertKit landing pages**, **Carrd**, **Tally**
forms.

**Korpha skills:** `affiliate.build_lead_magnet(niche, format)` ·
`affiliate.draft_welcome_sequence(niche, n_days)` ·
`affiliate.tag_subscribers(esp, rules)`.

### Phase 2 — Copywriting

**What:** Per-campaign swipe emails, bonus stack copy, social hooks,
webinar pitch (if doing affiliate webinar), pre-launch warm-up emails,
launch day emails (open / mid / close), post-launch follow-up.

**Who:** Affiliate Audience Manager → Copywriter agent specialized in
JV-launch swipes.

**3rd-party:** Vendor-supplied swipes (most launches give you swipes
to remix; remix is essential — don't copy-paste).

**Korpha skills:** `affiliate.write_swipe_email(launch, audience,
hook_style)` · `affiliate.write_bonus_stack_copy(launch, bonuses[])` ·
`affiliate.remix_vendor_swipe(vendor_swipe, voice_brief)`.

### Phase 3 — Order-Readiness

**What:** JV page (informational, not a checkout — the affiliate link
sends buyers to the vendor's funnel). Affiliate platform setup
(JVZoo, WarriorPlus, ClickBank). Bonus delivery infrastructure.

**Who:** Affiliate Line VP → Funnel Architect.

**3rd-party:** **JVZoo**, **WarriorPlus**, **ClickBank**, **PayKickStart**
for affiliate platforms. **Stripe** for bonus delivery (sometimes free,
sometimes paid bonus). **Beehiiv** / **ConvertKit** for delivery
sequences.

**Korpha skills:** `affiliate.build_jv_page(launch, audience, bonuses[])` ·
`affiliate.register_jv_link(platform, product_id)` ·
`affiliate.build_bonus_delivery_sequence(launch, buyer_segment)`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** Audience-specific. AI marketers audience →
X/Twitter + LinkedIn. KDP authors audience → BookTok + author Facebook
groups.

**4b GEO/AEO:** Indirect — affiliate-side benefits when LLMs cite
*your* recommendations. RankMyAnswer Line Pack here too.

**4c SEO:** Bonus stack page can rank for "[product] bonus" or "[product]
review." Easy wins for affiliate SEO.

**4d Platform listings as channel:** N/A.

**4e Paid ads:** **Risky in affiliate.** Many vendors require
PPC-traffic gates; some forbid Google Ads on brand terms. Read each
vendor's PPC policy before running ads to affiliate links.

### Phase 5 — Delivery

**What:** The *vendor* delivers the product. You deliver your *bonus
stack.* Hand-off happens immediately after Stripe webhook fires on the
vendor's side.

**3rd-party:** **PayKickStart / JVZoo / WarriorPlus** notify of sales;
your ESP triggers bonus delivery sequence.

**Korpha skills:** `affiliate.handle_sale_webhook(launch, buyer)` ·
`affiliate.deliver_bonus_stack(launch, buyer)`.

### Phase 6 — Customer Support

**What:** Vendor handles ALL product support. You handle:
- "Did I get my bonus?" (most common)
- "Which OTO should I get?" (pre-purchase)

**Autonomy level:** Level 3 (full autonomy — these are FAQ + lookup
questions).

**Korpha skills:** `support.lookup_bonus_delivery_status(buyer,
launch)` · `support.recommend_oto_for_buyer_segment(launch, buyer_profile)`.

### Affiliate walkthrough

> Affiliate Line VP's "AI marketers" Audience Manager has 12,400
> subscribers. A new launch invitation arrives.
>
> 1. **Niche compat check** via `niche.score_fit` — score 0.87, ACCEPT.
> 2. Vendor sends swipes → `affiliate.remix_vendor_swipe(swipes,
>    voice_brief="AI marketers audience")` produces 5 unique emails.
> 3. Bonus stack: 3 templates + a 30-min strategy call (priced at $0
>    incremental cost).
> 4. `affiliate.build_jv_page(launch, "AI marketers", bonuses)` →
>    JV-style bonus page lives on the audience's domain.
> 5. Pre-launch warm-up: 3 emails over 7 days hinting at the
>    upcoming launch.
> 6. Launch day: 3 emails (open / mid / close) over 4 days.
> 7. Webhook integration: every sale triggers
>    `affiliate.deliver_bonus_stack`.
> 8. Post-launch report: total commission earned, bonus delivery
>    rate, refund rate, audience health (unsubscribes? open-rate
>    impact?) → if any of those tip negative, `last_burned_at`
>    increments on the audience profile.

---

## Line 6: Agency Services

Agency is **service-billed + retainer-driven + deliverable-tracked.**
Different shape from product lines: you sell time + expertise, not
goods. Margin comes from leverage (templates, SOPs, AI agents doing
the work the agency historically billed for).

### Phase 1 — Create

**What:** Define service tiers (Starter / Pro / Enterprise), SOPs,
templates, intake questionnaires.

**Who:** Agency Line VP → Service Designer agent.

**3rd-party integrations:** **Notion** for SOPs + service catalog,
**ClickUp** / **Asana** for delivery management, **PandaDoc** /
**Bonsai** for contracts.

**Korpha skills:** `agency.define_service_tier(name, deliverables[],
price_model)` · `agency.author_sop(service, steps[])` ·
`agency.build_intake_form(service)`.

### Phase 2 — Copywriting

**What:** Service descriptions, proposal templates, onboarding emails,
weekly status report templates, case studies.

**Who:** Agency Line VP → Copywriter agent (B2B services voice).

**Korpha skills:** `agency.write_proposal(client, service, scope)` ·
`agency.write_case_study(completed_engagement)` ·
`agency.write_weekly_status_report(engagement)`.

### Phase 3 — Order-Readiness

**What:** Booking flow (discovery call → proposal → contract → payment
→ kickoff). Calendar booking + payment + contract + onboarding.

**Who:** Agency Line VP → Funnel Architect.

**3rd-party:** **Calendly** / **SavvyCal** for booking. **Stripe** /
**ThriveCart** for retainer / project payment. **PandaDoc** / **Bonsai**
for contracts. **Tally** / **Typeform** for intake.

**Korpha skills:** `agency.setup_booking_calendar(service, durations[],
buffer)` · `commerce.create_retainer_subscription(client, amount,
cadence)` · `agency.generate_contract(client, service, terms)`.

### Phase 4 — Eyeball-Getting

**4a Organic social:** **LinkedIn** is #1 for agencies. X/Twitter for
solopreneur agencies. **YouTube** long-form for high-ticket positioning.
Podcasts (host + guest appearances).

**4b GEO/AEO:** Critical for agencies. Clients ask AI "best agency for
X" — being cited matters. RankMyAnswer Line Pack.

**4c SEO:** Service-page SEO + case-study SEO. "Agency for X" + "X
consultant" keywords. **Skills:** `seo.research_agency_keywords(niche)`.

**4d Platform listings as channel:** **Upwork**, **Contra**, **Fiverr**
for entry-tier work. **Clutch**, **G2** for B2B agency leads.

**4e Paid ads:** **Google Search Ads** on high-intent ("X consultant
near me"). **LinkedIn Ads** for B2B. **Meta** less effective for
agency unless retargeting site visitors.

### Phase 5 — Delivery

**What:** Service delivery. Deliverables produced + status communicated +
client kept informed.

**Who:** Agency Line VP → Service Delivery agent + specialist workers.

**3rd-party:** **ClickUp** / **Asana** / **Linear** / **Notion** for
delivery management. **Loom** for async client updates. **Slack
Connect** for client comms.

**Korpha skills:** `agency.spawn_delivery_kanban(engagement, sop_id)` ·
`agency.send_weekly_status_report(engagement)` ·
`agency.record_loom_update(deliverable, message)`.

### Phase 6 — Customer Support

**What:** Continuous client communication. Slack DMs, weekly check-ins,
escalations, scope-change discussions.

**Autonomy level:** Level 1-2 (always draft for human approval — client
relationships are too high-stakes for full autonomy).

**3rd-party:** **Front**, **Missive**, **Slack Connect**, **Intercom**
for client comms.

**Korpha skills:** `agency.draft_client_reply(message, engagement_context)` ·
`agency.flag_scope_creep(client_request, original_sow)`.

### Agency walkthrough

> Agency Line VP runs "Korpha Setup & Optimization" service —
> done-for-you Korpha configuration for new customers.
>
> 1. Service tier defined: $2,500 one-time setup + $500/mo retainer.
> 2. Calendly booking for discovery call.
> 3. Proposal auto-generated post-call via `agency.write_proposal`.
> 4. Contract via PandaDoc API.
> 5. Stripe retainer subscription created.
> 6. Onboarding kanban auto-spawned with 12 cards (SOP-driven).
> 7. Workers (Dev agent, Setup agent, Onboarding agent) hired.
> 8. Weekly status reports auto-drafted, sent on Mondays.
> 9. Client portal accessible at a per-client subdomain.

---

## Cross-Cutting Concerns

### Build vs Integrate — the decision framework

At every phase, the Line VP picks between five implementation paths:

```
1. Use existing 3rd-party SaaS with API     ← default for solved problems
2. Wrap thin/quirky SaaS API with own skill ← when API works but is painful
3. Install a community Line Pack / skill    ← when the playbook is already packaged
4. Author a new skill                       ← `meta.author_python_skill` (#126)
5. Commission a custom mini-app             ← Codex CLI background run (#155)
```

**The wrap-the-thin-API case (option 2)** deserves its own slot because
it's so common. Many real-world SaaS APIs are:

- Inconsistently versioned (v1, v1.1, v2 — none deprecated)
- Returning unhelpful error shapes (HTML error pages on API endpoints)
- Rate-limited in unpredictable ways
- Missing batch endpoints (force you to N+1 individual calls)
- Auth flows that paginate strangely or expire silently

You still want the integration (avoid building Stripe from scratch),
but the raw API is friction. The right move: write a thin Korpha
skill that wraps the SaaS API, normalizes its quirks, and presents a
clean call surface to other agents. The skill becomes the moat for
working with that SaaS — and ships back to the skill hub as a community
contribution.

Examples in practice:
- KDP API has only manual upload for some operations; wrap with a
  Playwright-driven skill that handles the form-submission half
- Stripe v1 webhook signature verification has quirks; wrap with a
  single `stripe.verify_webhook(body, sig)` skill (we already did this)
- Etsy listing API: 13-tag limit + 140-char title + photo dimensions;
  wrap to enforce these client-side instead of round-tripping rejected
  API calls

**Decision rules** (in priority order):

| If… | Then… |
|-----|-------|
| A mature 3rd-party SaaS exists with a stable API + reasonable cost | **Integrate** — never build payment processing, email deliverability, calendar booking |
| The capability is the *moat* of YOUR product | **Build** — RMA's AEO scoring, Korpha's cofounder loop, your unique angle |
| The capability is a unique workflow with no existing SaaS | **Author skill** — niche-specific automation |
| The capability requires a custom UI on YOUR domain | **Build mini-app** — landing pages, JV pages, internal tools |
| Cost / control / ToS is at issue with 3rd-party | **Build** — when SaaS pricing is gouging or ToS-risky |

**Default-integrate categories** (don't build these unless you have a strong reason):

- Payment processing (Stripe, PayPal, Lemon Squeezy)
- Email sending + deliverability (Resend, SendGrid, Postmark)
- Hosting + CDN (Cloudflare, Vercel, Fly)
- Calendar booking (Cal.com, Calendly, SavvyCal)
- POD fulfillment (Printful, Printify)
- Helpdesk (Intercom, Crisp, HelpScout)
- Course hosting (Teachable, Kajabi)
- Affiliate platform (JVZoo, WarriorPlus)

**Default-build categories** (these define your business):

- Landing pages on your own domain
- Affiliate JV pages
- Custom dashboard for line-specific metrics
- Internal SOP runbooks
- Niche-specific automation that's your moat
- Skills the community hasn't packaged yet (yours to ship to the hub)

The Line VP should propose the path explicitly when starting work,
not silently choose. The founder approval flow surfaces "I'll use
Stripe for this" vs "I'll build a custom checkout."

### GEO/AEO — a sub-discipline of phase 4

GEO (Generative Engine Optimization) / AEO (Answer Engine Optimization)
is the practice of getting your content cited as the *answer* in
LLM responses. Every Line touches it; Korpha installs the
**RankMyAnswer Line Pack** (Andrew's other product) to get the
playbook.

**Why it's critical:**
- 58% of Google queries now show AI Overviews (Google's LLM-cited summary)
- ChatGPT processes 1B+ queries/day; many are "what's the best X for Y"
- Perplexity, Claude, Gemini all serve buyer-intent queries with citations
- Being cited = traffic. Not cited = invisible.

**Per-Line application:**
- **SaaS**: highest leverage. "Best AI for X" queries drive sign-ups
- **Info**: high leverage. "How do I X" queries surface course recommendations
- **Agency**: high leverage. "Best agency for X" queries surface inquiries
- **KDP**: moderate. Book-discovery via LLM is growing
- **Affiliate**: moderate. Review-style queries cite affiliate posts
- **POD**: low. POD buyers tend to query marketplaces, not LLMs

**Implementation:** Install the RMA Line Pack → it ships skills for
LLM-engine querying, citation triangulation, AEO score calculation,
schema markup generation. Korpha eats its own dogfood (uses RMA
to GEO-optimize itself).

### Customer Support Autonomy Ladder

Five levels of agent autonomy on customer support — each Line + sub-Line
picks its default level. The Line VP can elevate or lower depending on
risk:

| Level | Description | When appropriate |
|-------|-------------|------------------|
| **0 — Forward** | Agent reads message, files in queue, no reply | Never default; only for confused / sensitive cases |
| **1 — Draft for approval** | Agent drafts reply; human approves before sending | Negative reviews, refund requests, scope-change discussions |
| **2 — Auto-reply with threshold** | Agent auto-replies if confidence ≥ 0.8; else escalates | Mixed-confidence pre-sale questions |
| **3 — Full autonomy on FAQ** | Agent answers FAQ + does order lookups without escalation | POD order status, info-product "where is my login," SaaS doc lookup |
| **4 — Full autonomy + actions** | Agent can issue refunds (within rules), reset passwords, ship replacements | SaaS refund under $50, password reset, replacement on confirmed defect |

**Default by Line:**
- **POD**: Level 3 (FAQ + order lookup automated; marketplace handles most)
- **KDP**: Level 1-2 (drafts for approval — reputation-critical reviews; Level 2 with high-confidence threshold for non-controversial reader email)
- **Info**: split — Level 2 pre-sale (auto-reply with confidence threshold), Level 3 post-sale FAQ, Level 1 for refund requests
- **SaaS**: Level 3-4 (most mature — full autonomy on FAQ, refunds under $50 auto-approved, password reset auto)
- **Affiliate**: Level 3 (questions are mostly bonus-delivery status — FAQ + lookup)
- **Agency**: Level 1-2 (always draft for human approval — client relationships are high-stakes)

The autonomy level is configured per-unit in `BusinessUnit.config["support_autonomy_level"]`.

### 3rd-Party Integration Summary

Quick reference. Each tool is listed once even though it spans multiple Lines:

| Category | Tools |
|---|---|
| **LLM inference** | OpenAI, Anthropic, DeepSeek, Groq, Together, Cerebras, Cohere, Mistral, Z-AI, Moonshot, Nous, Hugging Face — 17 presets in env-fallback (#212) |
| **OAuth CLIs (local-mode only)** | Claude Code, Codex CLI, OpenCode, Cursor, Gemini CLI, ACPX, PI |
| **Image/audio AI (shared)** | Vidyo's GPU mesh: z-image-turbo, Whisper, Kokoro, OmniVoice, bg-removal |
| **Payment** | Stripe, PayPal, Square, Lemon Squeezy, Paddle, ThriveCart, SamCart |
| **Email/ESP** | Resend, SendGrid, Postmark, Mailgun, ConvertKit, Beehiiv, MailerLite, AWeber, GetResponse, Substack |
| **Hosting** | Cloudflare Pages, Vercel, Fly.io, Railway, Render, Netlify |
| **Database** | Supabase, Neon, Render, Railway, PlanetScale |
| **Affiliate platform** | JVZoo, WarriorPlus, ClickBank, PayKickStart |
| **Course hosting** | Teachable, Thinkific, Podia, Kajabi, MemberSpace, MemberStack, Skool |
| **POD fulfillment** | Printful, Printify, Merch by Amazon, Redbubble, Society6, TeePublic, Spreadshirt |
| **Calendar booking** | Cal.com, Calendly, SavvyCal, TidyCal |
| **Helpdesk** | Intercom, Crisp, HelpScout, Freshdesk, Zendesk, Front, Missive, Tidio |
| **Project management** | ClickUp, Asana, Linear, Notion, Trello, Monday |
| **Contracts** | PandaDoc, Bonsai, DocuSign, HelloSign |
| **Webinar** | WebinarJam, EverWebinar, Demio, Zoom Webinars |
| **Social scheduling** | Buffer, Hootsuite, Hypefury, Typefully, Publer, Tailwind (Pinterest) |
| **Ads platforms** | Meta Ads, Google Ads, LinkedIn Ads, TikTok Ads, Pinterest Ads, Amazon Ads, X Ads, Reddit Ads, BookBub Ads |
| **SEO/AEO** | RankMyAnswer (Andrew's own), Ahrefs, SEMrush, Frase, SurferSEO, Moz |
| **Marketplaces** | Amazon (KDP + Merch), Etsy, Gumroad, Lemon Squeezy, Udemy, Product Hunt, AlternativeTo, G2 |

---

## How Line Packs Implement This Document

Each Line Pack from the skill hub (#213) is the **packaged
implementation** of one Line's column in the summary matrix. A Line
Pack ships:

```yaml
# Example: kdp-romance-line-pack.yaml
pack_id: "kdp-romance@1.0.0"
line_kind: kdp
unit_kind: type
phases:
  create:
    skills: [kdp.outline_book, kdp.draft_chapter, kdp.generate_cover_concepts]
    integrations: [sudowrite, book_brush, omnivoice_clone]
  copy:
    skills: [kdp.write_book_description, kdp.write_aplus_content]
  order_readiness:
    skills: [kdp.publish_listing, kdp.set_kdp_select_or_wide]
    integrations: [kdp_api]
  eyeball:
    skills: [ads.amazon_kdp_sponsored_products, ads.bookbub_create_campaign]
    integrations: [amazon_ads, bookbub_ads]
  delivery:
    skills: []  # Amazon handles delivery
  support:
    autonomy_level: 1  # Draft for approval
    skills: [support.draft_reply_to_reader]
niche_profile_defaults:
  core_topics: [romance, fiction, kindle_unlimited]
  off_limits_topics: [non_fiction, business, finance]
kpi_definitions: [...]
kanban_templates: [...]
```

Installing the pack into a `BusinessUnit` auto-configures the playbook,
preferred skills, niche profile, KPIs, and kanban templates for that
phase × line combination.

This is what makes the Line Pack marketplace defensible: real
operators who run KDP Romance / Etsy POD / Affiliate launches package
their playbooks, sell them, and other solopreneurs install the
expertise rather than having to learn it from scratch.

---

## See Also

- [`docs/ORG_MODEL.md`](./ORG_MODEL.md) — who works for whom
- [`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) — engineering
- Skill hub (#213) — Line Pack packaging + install machinery
- Inference provider plugin (#123) — extends to non-LLM AI models
- Memory namespacing (in BUSINESS_UNITS.md) — keeps Line audiences
  from cross-polluting
- Per-unit credentials (in BUSINESS_UNITS.md) — separate Stripe per
  Line for tax + P&L attribution
