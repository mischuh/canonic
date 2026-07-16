# Changelog

## [0.8.3](https://github.com/mischuh/canonic/compare/v0.8.2...v0.8.3) (2026-07-16)


### Documentation

* add initial HTML structure and favicon for the canonic website to root ([#224](https://github.com/mischuh/canonic/issues/224)) ([c4720ec](https://github.com/mischuh/canonic/commit/c4720eca329857611002fe47c80db5f86f03447d))

## [0.8.2](https://github.com/mischuh/canonic/compare/v0.8.1...v0.8.2) (2026-07-16)


### Documentation

* add initial HTML structure and favicon for the canonic website ([#222](https://github.com/mischuh/canonic/issues/222)) ([b94d9fa](https://github.com/mischuh/canonic/commit/b94d9fad9bbc9548d45073f8b8b01fc1b0f8cd14))

## [0.8.1](https://github.com/mischuh/canonic/compare/v0.8.0...v0.8.1) (2026-07-16)


### Bug Fixes

* **compiler:** rewrite SQLite 'start of X' date modifiers for Redshift/Postgres ([#220](https://github.com/mischuh/canonic/issues/220)) ([1d33dd9](https://github.com/mischuh/canonic/commit/1d33dd99c56fc58408ce8dff237ccdd2ee7023d4))

## [0.8.0](https://github.com/mischuh/canonic/compare/v0.7.3...v0.8.0) (2026-07-16)


### Features

* **cli:** add ingest add-tables subcommand ([72c5791](https://github.com/mischuh/canonic/commit/72c5791eaee765f6eaacc9d1812792c3dddc6356))

## [0.7.3](https://github.com/mischuh/canonic/compare/v0.7.2...v0.7.3) (2026-07-14)


### Documentation

* fix docs review findings and back the intro example with real output ([#216](https://github.com/mischuh/canonic/issues/216)) ([5821097](https://github.com/mischuh/canonic/commit/582109767940594f2674bac38f6ffe2de8c7542d))

## [0.7.2](https://github.com/mischuh/canonic/compare/v0.7.1...v0.7.2) (2026-07-14)


### Bug Fixes

* **mcp:** spawn http daemon via fork+exec instead of bare os.fork() ([#214](https://github.com/mischuh/canonic/issues/214)) ([c37abbf](https://github.com/mischuh/canonic/commit/c37abbf19867644f5b6a57a6b0a3caaca9a097f6))

## [0.7.1](https://github.com/mischuh/canonic/compare/v0.7.0...v0.7.1) (2026-07-14)


### Bug Fixes

* **docker:** add QEMU setup step and specify platforms for Docker build ([7690baa](https://github.com/mischuh/canonic/commit/7690baab3f3b7441bd28ad078288889d46b98c81))
* **docker:** add QEMU setup step and specify platforms for Docker build ([4c70af7](https://github.com/mischuh/canonic/commit/4c70af740774942318974b0fc066450dbead70d8))

## [0.7.0](https://github.com/mischuh/canonic/compare/v0.6.0...v0.7.0) (2026-07-13)


### Features

* **embeddings:** Wire embeddings into knowledge search with a persis… ([cc3eb17](https://github.com/mischuh/canonic/commit/cc3eb175453a3d4a4a164fc57da04cd2638fcc84))
* **embeddings:** wire embeddings into knowledge search with a persistent vector cache ([50bf994](https://github.com/mischuh/canonic/commit/50bf994c5fd8e3700b9fd06caa3488e40d49d484))


### Bug Fixes

* **embeddings:** resolve mypy errors from huggingface_hub logging suppression ([600de34](https://github.com/mischuh/canonic/commit/600de34eb46d5c67b6271630a442cafbbb00fd12))

## [0.6.0](https://github.com/mischuh/canonic/compare/v0.5.3...v0.6.0) (2026-07-12)


### Features

* **mcp:** add bearer-token auth for --transport http ([01f5cca](https://github.com/mischuh/canonic/commit/01f5cca56511679f6adda4b9260019d1977a3900))
* **mcp:** add bearer-token auth for --transport http ([7b2376a](https://github.com/mischuh/canonic/commit/7b2376aee10f656c267c47192acb6c1a870a0acd))


### Bug Fixes

* clarify documentation for unimplemented features and update signal descriptions ([ba1fd23](https://github.com/mischuh/canonic/commit/ba1fd23002422dcf685067c67788422ac6b2b6ec))
* update knowledge search command and tests for clarity and functionality ([ebd2180](https://github.com/mischuh/canonic/commit/ebd218068ca8f7d4bb113ff8f72e142f5bb59753))

## [0.5.3](https://github.com/mischuh/canonic/compare/v0.5.2...v0.5.3) (2026-07-10)


### Bug Fixes

* stop LLM from auto-picking ambiguous join paths ([6761375](https://github.com/mischuh/canonic/commit/6761375d302ba5c8a6795f31453765b5a28ce9e8))
* stop LLM from auto-picking ambiguous join paths ([c05bad7](https://github.com/mischuh/canonic/commit/c05bad79ba2642bd6dde7d6a35dc896b8d13b552))

## [0.5.2](https://github.com/mischuh/canonic/compare/v0.5.1...v0.5.2) (2026-07-10)


### Documentation

* document --project, env passthrough, and --suggestions flag for MCP setup ([55734f2](https://github.com/mischuh/canonic/commit/55734f21764656c45ae8605390526e9611fe2eec))
* enhance MCP client configuration instructions and add environment variable handling ([208d8cb](https://github.com/mischuh/canonic/commit/208d8cbfd50e76a73d0a26294027f37d414cd50a))
* update MCP configuration instructions for uvx canonic setup ([ba4720f](https://github.com/mischuh/canonic/commit/ba4720f48a348c8d91fad997059177876b58c3fd))
* update MCP configuration instructions for uvx canonic setup ([5d6b379](https://github.com/mischuh/canonic/commit/5d6b379011cf65b90d9480793b515949be58a38c))

## [0.5.1](https://github.com/mischuh/canonic/compare/v0.5.0...v0.5.1) (2026-07-10)


### Documentation

* update README links to point to the correct URLs ([ebc7b02](https://github.com/mischuh/canonic/commit/ebc7b0264953388e0b74433ddba70f03de5d5313))
* update README links to point to the correct URLs ([4ed3ecb](https://github.com/mischuh/canonic/commit/4ed3ecb0046e276e32268d9b5f10cc56f6fa8951))

## [0.5.0](https://github.com/mischuh/canonic/compare/v0.4.0...v0.5.0) (2026-07-09)


### Features

* **feedback:** add E11 answer feedback loop ([3238132](https://github.com/mischuh/canonic/commit/3238132d3bdd4d4f4c4ea6fafaadd33a52180f10))
* **instrumentation:** add E16 Part 2 full instrumentation ([bced9e6](https://github.com/mischuh/canonic/commit/bced9e634dfddb970d3b302f97856511f571af50))
* **trust:** add E14 answer trust score and min_trust guardrail ([5c42da2](https://github.com/mischuh/canonic/commit/5c42da222053107352477c6176b03c9e366adce3))


### Documentation

* cover E14 trust score, E16 schema bump, and E11 feedback loop ([3d312a7](https://github.com/mischuh/canonic/commit/3d312a7d08da71da70c72d41e6e9b91275b490a5))
* update CONTRACT_CHANGELOG with new version 2.2 and summary for E14 trust tier logging ([5a89dda](https://github.com/mischuh/canonic/commit/5a89dda26ce0e2f034b0a41d801af38427e2a125))

## [0.4.0](https://github.com/mischuh/canonic/compare/v0.3.4...v0.4.0) (2026-07-08)


### Features

* **distribution:** switch to Python-only distribution (uv/PyPI, pip, Docker) ([410643d](https://github.com/mischuh/canonic/commit/410643db04e5c51f83ac4ecd5e9e3e49675b5c32))


### Bug Fixes

* add commitlint configuration extending conventional config ([a000232](https://github.com/mischuh/canonic/commit/a000232804a3ef75758690c51d07b4c5e397c95b))
* **compiler:** qualify dimension output aliases by join role to avoid column collisions ([6785fd7](https://github.com/mischuh/canonic/commit/6785fd7001bd107c3b35deb571a9db1640b44fa0))

## [0.3.4](https://github.com/mischuh/canonic/compare/v0.3.3...v0.3.4) (2026-07-08)


### Bug Fixes

* fall back to CUME_DIST() window query for percentile on SQLite ([4851aad](https://github.com/mischuh/canonic/commit/4851aadcd4f426acbb9db1f32dfe6c7957f9ffd8))
* fall back to CUME_DIST() window query for percentile on SQLite ([d92d301](https://github.com/mischuh/canonic/commit/d92d3015109acc34e72eec50f30328649d7cf95b))

## [0.3.3](https://github.com/mischuh/canonic/compare/v0.3.2...v0.3.3) (2026-07-07)


### Bug Fixes

* mark bootstrap-generated metric contracts as human_curated ([b01e622](https://github.com/mischuh/canonic/commit/b01e622409e5919cb33dd7c053369b06bd2faf35))


### Documentation

* add detailed schema references across contract, knowledge, and semantic documentation ([9a48b56](https://github.com/mischuh/canonic/commit/9a48b56553763046a03eaca15c032653ef2ac8e5))
* add end-to-end example and update quickstart with reference ([c2e222a](https://github.com/mischuh/canonic/commit/c2e222ad6d7823ee26fc3d0c2a2e4f89f56d33bc))
* enhance `canonic eval` documentation with detailed usage and examples ([706f43f](https://github.com/mischuh/canonic/commit/706f43f6721e1de37c372883dfde6fb0a60e3a73))
* refactor readme files for jaffle shop, rental, and saas analytics examples ([99ae23b](https://github.com/mischuh/canonic/commit/99ae23b52f90ebc8404bef533d37e24511c82709))
* update README with examples of canonic's impact on data accuracy ([8500a4f](https://github.com/mischuh/canonic/commit/8500a4f6e26b9fe3e42efb729bf79193a4ad2125))

## [0.3.2](https://github.com/mischuh/canonic/compare/v0.3.1...v0.3.2) (2026-07-06)


### Documentation

* remove CI/CD section, add MCP HTTP config example ([3b542a4](https://github.com/mischuh/canonic/commit/3b542a48cf94b8d937c21d350036bc94f37256fb))
* remove outdated CI/CD documentation and update error codes description ([4ba1d70](https://github.com/mischuh/canonic/commit/4ba1d703b736f054e1dc50e9d6cdaf38b7ecffea))

## [0.3.1](https://github.com/mischuh/canonic/compare/v0.3.0...v0.3.1) (2026-07-06)


### Documentation

* add demo setup GIF and script for quickstart guide ([8593bab](https://github.com/mischuh/canonic/commit/8593babb0a06f091940d0b50952685ada329fbc6))
* **intro:** add concrete before/after example showing Canonic's value ([59ef6a4](https://github.com/mischuh/canonic/commit/59ef6a4027cf4d6dfbf3eb8c63a903da075f9bc8))

## [0.3.0](https://github.com/mischuh/canonic/compare/v0.2.3...v0.3.0) (2026-07-06)


### Features

* **cli:** add inline --metrics/--dimensions/--filter flags to query/sl compile ([abe2d4f](https://github.com/mischuh/canonic/commit/abe2d4f06845ee0b17e572b588e3be490869e1fa))
* **cli:** add inline --metrics/--dimensions/--filter flags to query/sl compile ([7faafdf](https://github.com/mischuh/canonic/commit/7faafdf39ca697b4044ed84b6575ab2bbc407a1a))

## [0.2.3](https://github.com/mischuh/canonic/compare/v0.2.2...v0.2.3) (2026-07-05)


### Documentation

* remove LookML, correct MCP tool count, extend quickstart ([7f2d64d](https://github.com/mischuh/canonic/commit/7f2d64d13283f6fb00135fc0353fa4322bde6b1a))
* update connector and semantic documentation for clarity and extensibility ([b3ce4b3](https://github.com/mischuh/canonic/commit/b3ce4b3658bf8e05ec87666357c45ca7a93fd6a7))

## [0.2.2](https://github.com/mischuh/canonic/compare/v0.2.1...v0.2.2) (2026-07-05)


### Bug Fixes

* **docker,publish:** fix Dockerfile build and disable broken npm-publish job ([6122649](https://github.com/mischuh/canonic/commit/6122649881a32d4c6fc3b943774aebd5c0e76001))
* **docker,publish:** fix Dockerfile build and disable broken npm-publish job ([33b106b](https://github.com/mischuh/canonic/commit/33b106b1aac33c664bd74ae827c8a1528c802339))

## [0.2.1](https://github.com/mischuh/canonic/compare/v0.2.0...v0.2.1) (2026-07-05)


### Bug Fixes

* **ci:** reference the actual secret name for release-please's token ([780a606](https://github.com/mischuh/canonic/commit/780a606631b859da11a5a7adb721e682f4b64aaa))
* **ci:** reference the actual secret name for release-please's token ([1a5bd6b](https://github.com/mischuh/canonic/commit/1a5bd6bafe006647fd74e6943ce4d6d6822745ff))
* **ci:** use a PAT for release-please, and gitignore specs/*.md ([5ebb368](https://github.com/mischuh/canonic/commit/5ebb36856e17c5f31a08cb3cf130071d5569b730))
* **ci:** use a PAT for release-please, and gitignore specs/*.md on this branch ([3ce2839](https://github.com/mischuh/canonic/commit/3ce2839cf21e3f886f2e0072ee80b439b820b27c))

## [0.2.0](https://github.com/mischuh/canonic/compare/v0.1.0...v0.2.0) (2026-07-05)


### Features

* add canon review and canon apply commands (GH-150) ([4871596](https://github.com/mischuh/canonic/commit/48715967b2c877fa64057e7e79868c7b793b9e22))
* add ecommerce example and fix HTTP daemon fork ([a3488d9](https://github.com/mischuh/canonic/commit/a3488d95d55490cc11c39ee3d99ba83901f4bbbd))
* add outside_project fixture for testing outside project directories and update status dimension label ([b9ce5be](https://github.com/mischuh/canonic/commit/b9ce5be4d2cdda4608d00102ee861860969a2531))
* add Redshift connector ([22f17e1](https://github.com/mischuh/canonic/commit/22f17e1e7b061df0b85aac50fda1b2ad6ed2f899))
* add SQLite connector with full P0 capability suite ([0e482a9](https://github.com/mischuh/canonic/commit/0e482a984c034e7dd07169e0849a8327ce7f32e7))
* **ci:** enforce Conventional Commits with commitlint ([53be410](https://github.com/mischuh/canonic/commit/53be41026f488b8f6d554727757c7088c29c982e))
* **ci:** guard contract_schema changes against a missing changelog entry ([d573cc1](https://github.com/mischuh/canonic/commit/d573cc14d68ba30b5c65caec79b9656a9b457f90))
* CLI skeleton — Typer command tree + headless exit-code contract (GH-6) ([552e0e3](https://github.com/mischuh/canonic/commit/552e0e37c18e48c48dfb60ad47cb430b057182c9))
* CLI skeleton — Typer command tree + headless exit-code contract… ([db61d8e](https://github.com/mischuh/canonic/commit/db61d8eeb5b02520b658e4ee686466fc38535fdc))
* **cli:** implement bare canon interactive mode (SPEC-E7-E8 §3) ([d04decd](https://github.com/mischuh/canonic/commit/d04decd8a72212144ec4b838eff78b5b6b167950))
* **cli:** implement canon sl resolve and sl compile (SPEC-E7-E8 §3) ([f4ae1f1](https://github.com/mischuh/canonic/commit/f4ae1f1ddbbfa8bffc1a7e77e007665d256c8e2b))
* **cli:** implement connection commands ([2b4f7bb](https://github.com/mischuh/canonic/commit/2b4f7bb83659a36ef6695ad6bf1cb74948f02d30))
* compiler core — deterministic semantic query → Postgres SQL (GH-10) ([3da211c](https://github.com/mischuh/canonic/commit/3da211c5eff8d150a532bcfa54b090a52c4d8869))
* compiler core — deterministic semantic query → Postgres SQL (GH… ([5f9935f](https://github.com/mischuh/canonic/commit/5f9935f6affe5e1200b227c1b753324f44c43ecc))
* **compiler:** add `via` to resolve ambiguous join paths + improve error UX ([7641e0f](https://github.com/mischuh/canonic/commit/7641e0f282423a24f06640ea4175c67ce33e99ba))
* **compiler:** conservative finality composition for ratio/weighted_avg (GH-122) ([5d01b00](https://github.com/mischuh/canonic/commit/5d01b005058f7f652afbbc500e7945b961b5ed5d))
* **compiler:** E15 composable_post_agg — ratio & weighted_avg (GH-118) ([2a71870](https://github.com/mischuh/canonic/commit/2a7187070cc492da3b768c0b6e2295dbf14b6eda))
* **compiler:** E15 composable_post_agg — ratio & weighted_avg (GH-118) ([0048bf3](https://github.com/mischuh/canonic/commit/0048bf314b3d94fc9ff02409b24bc4b3365c12e6))
* **compiler:** E15 opaque — grain-locked pre-computed values (GH-121) ([6035fe0](https://github.com/mischuh/canonic/commit/6035fe07155c6120b5a3a4b450a951a010df08d3))
* **compiler:** E15 opaque — refine grain guard, add _build_opaque, expand tests (GH-121) ([1b9b5f9](https://github.com/mischuh/canonic/commit/1b9b5f9e5ef8a0a5802a40fd23de239e7fceb510))
* **compiler:** E15 partial_additive — semi_additive binding kind (GH-119) ([45d254d](https://github.com/mischuh/canonic/commit/45d254dbd921302e1ad6121c3857a97961da6c3e))
* **compiler:** E15 population_filter — one population-restriction mechanism across all kinds (GH-128) ([367f5b0](https://github.com/mischuh/canonic/commit/367f5b0f53c692528bd8ddce0784171714c71b4a))
* **compiler:** E15 population_filter (GH-128) ([b90c869](https://github.com/mischuh/canonic/commit/b90c869201f230ed995b4e068adc9ec8a81286a2))
* **compiler:** E15 recompute_at_grain — distinct_count & percentile (GH-120) ([db7885d](https://github.com/mischuh/canonic/commit/db7885d7267132cfb9630eca62da3b1ca7fded13))
* **compiler:** E15 recompute_at_grain — distinct_count & percentile (GH-120) ([619bcc4](https://github.com/mischuh/canonic/commit/619bcc4183682507c38ceec64cf7552e2ec61f8c))
* **compiler:** E15 safety floor — reject-if-corrupting for non-additive (GH-117) ([e304087](https://github.com/mischuh/canonic/commit/e304087f3b71b4ac7e5663f8e948e621b6baaa24))
* **compiler:** E15 safety floor — reject-if-corrupting for non-additive measures (GH-117) ([94034f0](https://github.com/mischuh/canonic/commit/94034f0c179f4e5f8f4ccadd3ecb4cc288c29ae7))
* **compiler:** E15-add-S6 conservative finality composition for ratio/weighted_avg (GH-122) ([bd5b428](https://github.com/mischuh/canonic/commit/bd5b4288fedd626334017fe71c48a928862abd13))
* **compiler:** role-qualified joins + actionable ambiguous-path errors ([5a66237](https://github.com/mischuh/canonic/commit/5a6623721c2390bd2a43c9586a9dd02f92fd4e28))
* connector contract & normalized evidence schema (GH-3) ([8178c25](https://github.com/mischuh/canonic/commit/8178c252b243caf98a94467df7071fe05797a97a))
* connector contract & normalized evidence schema (GH-3) ([7185c40](https://github.com/mischuh/canonic/commit/7185c40e3db90b310643ff41a4e1d263679c0614))
* **connectors:** add LLM-backed default extraction skill for evidence connectors ([0eb11d4](https://github.com/mischuh/canonic/commit/0eb11d475d279eab7b738adcb61ace9e4818b7b1))
* **connectors:** split Notion evidence connector into fetch/extract ([0408514](https://github.com/mischuh/canonic/commit/0408514bf2dc3907e6c5b0ec9cb135685f82d418))
* contract surface — metric bindings, guardrails & cross-surface validation (GH-8) ([973d230](https://github.com/mischuh/canonic/commit/973d230ab63c18b7111adba0fb1dfdeabbf3789b))
* contract surface — metric bindings, guardrails & cross-surface validation (GH-8) ([c79f1e8](https://github.com/mischuh/canonic/commit/c79f1e8d808a237beb2cbfa0af92a085aa992e2a))
* ContractResolver — canonicality authority for compiler (GH-9) ([5b10e00](https://github.com/mischuh/canonic/commit/5b10e004b943f322637ee8dfd1a1657179bf5bb7))
* ContractResolver — canonicality authority for compiler (GH-9) ([0e57f4c](https://github.com/mischuh/canonic/commit/0e57f4caf1641bbcdfff4bf7c27e9bba1424de4b))
* **contracts:** validate population_filter columns against all leaf sources on write (GH-123) ([6c6f0b2](https://github.com/mischuh/canonic/commit/6c6f0b26de8dc827927a55adaeb33268b8a8cf33))
* **contracts:** validate population_filter columns against all leaf sources on write (GH-123) ([9bed890](https://github.com/mischuh/canonic/commit/9bed89002bf5cd8c605f9711d5d65348cb03d753))
* DuckDB connector + Jaffle Shop example ([a61533c](https://github.com/mischuh/canonic/commit/a61533cc011cfcec3c49ee3a2bda92c5cac221a8))
* DuckDB connector + Jaffle Shop example ([e6ef51d](https://github.com/mischuh/canonic/commit/e6ef51d152603c1eeb374a3686f88fedf6832c8d))
* **e10:** air-gapped enforcement — load-time + call-time egress blocking (GH-63) ([6f136e3](https://github.com/mischuh/canonic/commit/6f136e3344e1d5ed397501091e0646a794fcf115))
* **e10:** air-gapped enforcement (GH-83) ([fc1e194](https://github.com/mischuh/canonic/commit/fc1e194b87263a9c72d6e9efcfc588f02aab1957))
* **e10:** call-time api_key_ref resolution — secret never stored on instance (GH-65) ([1dac081](https://github.com/mischuh/canonic/commit/1dac08112fa6c6abe662b8623d567708a7228993))
* **e10:** call-time api_key_ref resolution (GH-65) ([1926054](https://github.com/mischuh/canonic/commit/1926054ccc7e88c567cbd49ad4d77b391a6eb53d))
* **e10:** local embedding runtime — sentence-transformers, model identity, reindex signal (GH-64) ([909c942](https://github.com/mischuh/canonic/commit/909c942364b6a187238331e3fee067d5915d332d))
* **e10:** local embedding runtime (GH-64) ([0ffc033](https://github.com/mischuh/canonic/commit/0ffc0334988981dcc648710b73664fd3d363ee0c))
* **e10:** local-model baseline harness — canon eval baseline, draft accuracy + structured-output behavior (GH-66) ([f6e358e](https://github.com/mischuh/canonic/commit/f6e358e4c91744df1ce33ffeeb4969e9a8264f11))
* **e10:** local-model baseline harness (GH-66) ([1f597e5](https://github.com/mischuh/canonic/commit/1f597e527945892b1cf4c5ab0495bcd431774de9))
* **e10:** modes & determinism — headless off, interactive on, no-models valid (GH-68) ([0104e56](https://github.com/mischuh/canonic/commit/0104e56c017074d563d5f51ee6f0ebb26dc07ac0))
* **e10:** modes & determinism (GH-68) ([3ec7a2e](https://github.com/mischuh/canonic/commit/3ec7a2ed893c91077b7f62bae57c3ea0238785ec))
* **e10:** provider abstraction — litellm-backed GenerationRuntime (GH-61) ([826a33c](https://github.com/mischuh/canonic/commit/826a33c6133da7f8a523cb753a61ccb244474431))
* **e10:** provider abstraction — litellm-backed GenerationRuntime, openai_compatible path, structured output (GH-61) ([f2b3869](https://github.com/mischuh/canonic/commit/f2b3869188b58917946e183196e77b36544c1107))
* **e10:** task-based model routing — Task enum, bounded retries (GH-62) ([d40f1d4](https://github.com/mischuh/canonic/commit/d40f1d4db6dc09a69a2b2264b96cfe0df0c42331))
* **e10:** task-based model routing — Task enum, bounded retries (GH-62) ([43278db](https://github.com/mischuh/canonic/commit/43278dba3805a9cf81319565be8f41b95b4e7810))
* **e10:** usage metrics + RetriesExhausted error variant (GH-67) ([21b0433](https://github.com/mischuh/canonic/commit/21b0433755bea2f18d45d59c6fe054ceb4e5f08c))
* **e10:** usage metrics + RetriesExhausted error variant (GH-67) ([ceda8b2](https://github.com/mischuh/canonic/commit/ceda8b26cdeecf0e56cfb754fcdd1ace1f9a9560))
* **E15-S1:** finality source selection & coalescing (compiler stages 5 + 8) ([31b1066](https://github.com/mischuh/canonic/commit/31b106660b2f2c59ed210bed967a363537d66bd7))
* **E15-S1:** finality source selection & coalescing (GH-107) ([1b33907](https://github.com/mischuh/canonic/commit/1b339072ecf798fddec6f15afa23929fff903237))
* **E15-S2:** restrict-source guardrail enforces final-only in context (GH-108) ([41482ed](https://github.com/mischuh/canonic/commit/41482edc9690c0cd3a3c2cdb9244144dcc319478))
* **E15-S3:** assertion execution + ASSERTION_FAILED structured error (GH-109) ([98c3a7f](https://github.com/mischuh/canonic/commit/98c3a7f6f8c034703c9376867f67ad3af1611abf))
* **E15-S3:** assertion execution + ASSERTION_FAILED structured error (GH-109) ([86797cd](https://github.com/mischuh/canonic/commit/86797cd4ac3b577bf3e48abdcef6ff587c6200a6))
* **E15-S4:** accuracy harness integration — assertions as the oracle (GH-110) ([ced5a91](https://github.com/mischuh/canonic/commit/ced5a91aeb9e24af49cfb03ba4544c2fa12680c2))
* **E15-S4:** accuracy harness integration — assertions as the oracle (GH-110) ([810dd4a](https://github.com/mischuh/canonic/commit/810dd4a5f24ba643a235d0a6a3b0e196c723ec90))
* **E15-S5/S6:** surface measure drift as prose-review flags at serve time (GH-111) ([58c37b2](https://github.com/mischuh/canonic/commit/58c37b2aecca4571a6b3812dc7e0e34b9e0d5698))
* **E15-S5/S6:** surface measure drift as prose-review flags at serve time (GH-111) ([d65b9a3](https://github.com/mischuh/canonic/commit/d65b9a3a3a478d9b4fbc9b8f9141878c70cf37fa))
* **E16-P1:** update the examples with new features ([ac3b066](https://github.com/mischuh/canonic/commit/ac3b066b23dd0107a63540429079e1a19734a5b6))
* **E16-S1:** append AnswerEvent to local event log (GH-77) ([2ecab31](https://github.com/mischuh/canonic/commit/2ecab3197270f95d3b054cfe583f97ff5c4baf07))
* **E16-S1:** append AnswerEvent to local event log on every served answer (GH-77) ([10cd07a](https://github.com/mischuh/canonic/commit/10cd07a81fe75c3485b3f5087f538222e4967b9a))
* **E16-S2:** add canon report command and event-log read path (GH-78) ([d2c478d](https://github.com/mischuh/canonic/commit/d2c478d3a053d06f6f9b7185a611bbac79a91a70))
* **E16-S2:** add canon report command and event-log read path (GH-78) ([0ab8512](https://github.com/mischuh/canonic/commit/0ab85123b96bc8934415221aa676af79ced64f3a))
* **E16-S3:** freeze AnswerEvent v1 schema (GH-79) ([0d53610](https://github.com/mischuh/canonic/commit/0d53610bf6fc7eddc44dce9b2091cd2f0b0a4320))
* **E16-S3:** freeze AnswerEvent v1 schema and prove reserved fields round-trip (GH-79) ([7c0b323](https://github.com/mischuh/canonic/commit/7c0b3237b953db7d2f0e7895acb243b606a9c01a))
* **E16-S4:** unify served_answer and reconcile_decision (GH-80) ([ca687bb](https://github.com/mischuh/canonic/commit/ca687bb4d548ae37c80b32e34583576ccfe32979))
* **E16-S4:** unify served_answer and reconcile_decision into one local event log ([54f39d0](https://github.com/mischuh/canonic/commit/54f39d06e89309565567b6b2bfc92607a4e20ce8))
* **E16-S5:** extract guard_telemetry chokepoint and add AC1/AC2 tests (GH-81) ([22db55e](https://github.com/mischuh/canonic/commit/22db55e02217d8a3803a09a2838c5ff1c5b8a881))
* **E2-S9:** introduce ConnectorFactory class and UnknownConnectorType (GH-102) ([cf15b56](https://github.com/mischuh/canonic/commit/cf15b5606cd6976e414ca65ea015e172169ff356))
* **E2-S9:** introduce ConnectorFactory class and UnknownConnectorType (GH-102) ([895bec9](https://github.com/mischuh/canonic/commit/895bec994ce6873fab8808d00ff5d9fc33a780db))
* **E3-S1:** dbt manifest connector → DefinitionEvidence + RelationSchema (GH-87) ([7ea4101](https://github.com/mischuh/canonic/commit/7ea41013a76bcbcd32be09fafe9b47cbf73e9a06))
* **E3-S1:** dbt manifest connector → DefinitionEvidence + RelationSchema (GH-87) ([16b7ce8](https://github.com/mischuh/canonic/commit/16b7ce8a400a88017635e7666846eb0b703c3c35))
* **E3-S2:** Notion pages → normalized DocEvidence (GH-88) ([dbb06e2](https://github.com/mischuh/canonic/commit/dbb06e295dde8f8eb0c159ddd982f6ccb5eced8a))
* **E3-S2:** Notion pages → normalized DocEvidence (GH-88) ([5378700](https://github.com/mischuh/canonic/commit/537870039f4d8b07c722b8981232e75333333441))
* **E3-S3:** Metabase + Looker → UsageEvidence (GH-89) ([58a5e1f](https://github.com/mischuh/canonic/commit/58a5e1fab08f91fd1cb4613891a91ff7052cc096))
* **E3-S3:** Metabase + Looker → UsageEvidence with FR-13 role enforcement (GH-89) ([742ee59](https://github.com/mischuh/canonic/commit/742ee5953bda4f6efbbf57348dbc5d868d28ce1a))
* **E3-S4:** capability dispatch + conformance harness (GH-90) ([8d99008](https://github.com/mischuh/canonic/commit/8d99008fbb25526e64d046802491b1643f7903a7))
* **E3-S4:** capability dispatch + conformance harness (GH-90) ([94334f1](https://github.com/mischuh/canonic/commit/94334f141473200381c6bf88e7b14e8e2dca0608))
* **E3-S5:** version pinning fails loudly with no partial ingest (GH-91) ([bc38c63](https://github.com/mischuh/canonic/commit/bc38c6356565b3d01ac557647fbbbad0824be762))
* **E3-S5:** version pinning fails loudly with no partial ingest (GH-91) ([e9bef46](https://github.com/mischuh/canonic/commit/e9bef46ebd3f72bf7de4dfc554eb1147c62e4fec))
* **E3-S6:** modeling-tier outranks raw introspection (GH-92) ([7eb9053](https://github.com/mischuh/canonic/commit/7eb905324ae360317c2633ddce2c6e2e37e64ff2))
* **E3-S6:** modeling-tier outranks raw introspection; type conflicts as contradictions (GH-92) ([88876ef](https://github.com/mischuh/canonic/commit/88876efb2555e7b5b540a63fc119f2e63f2cca41))
* **E3-S7:** enforce normalized seam (GH-93) ([643762f](https://github.com/mischuh/canonic/commit/643762fd9e4eef015b7c92aea186c1f9446036d9))
* **E3-S7:** enforce normalized seam; log and drop unknown/invalid evidence kinds ([3cda0a5](https://github.com/mischuh/canonic/commit/3cda0a5cb38958601e7aa1b5c4ed083e1ee4d971))
* **E3-S8:** enforce no-execution invariant for E3 connectors (GH-94) ([f03928d](https://github.com/mischuh/canonic/commit/f03928ddc29ee15deaa48749f5fa0670b02c24d1))
* **E4/E10:** wire reconcile call site end-to-end ([89d8bfd](https://github.com/mischuh/canonic/commit/89d8bfd180f6f9732635c4b0a987e9b13b1b6226))
* **e4:** add diff emitter & audit trail (GH-35) ([549a90d](https://github.com/mischuh/canonic/commit/549a90dad1df54bf1c20b97298ccc026fe8c2896))
* **e4:** add ingestion data models — EvidenceItem, Proposal, ReconciliationReport (GH-32) ([68fa8a1](https://github.com/mischuh/canonic/commit/68fa8a13f9a18fb7a91509c83aa8ec8652f4166d))
* **e4:** add reconciliation engine (GH-34) ([a6e445c](https://github.com/mischuh/canonic/commit/a6e445c59f0b20af0c2ba53a3e4126b1ef272c5f))
* **e4:** add reconciliation engine (GH-34) ([aca6c99](https://github.com/mischuh/canonic/commit/aca6c99b8e10f465082a2b8f4290825900d18e17))
* **e4:** add validation gate — schema probe + semantic validation before diff emit (GH-36) ([b0d4a0c](https://github.com/mischuh/canonic/commit/b0d4a0c0856d8b4a515dedd48a08cc249c8dfcc4))
* **e4:** add validation gate (GH-36) ([58b36ff](https://github.com/mischuh/canonic/commit/58b36ff61a4da2a5843b1a53c760f15b157652f9))
* **e4:** attach usage-backed examples to canonical bindings at reconciliation time (GH-156) ([e351acf](https://github.com/mischuh/canonic/commit/e351acfdaaa7d18ff7afa1ebaa47558c591f08ac))
* **e4:** attach usage-backed examples to canonical bindings at reconciliation time (GH-156) ([6121700](https://github.com/mischuh/canonic/commit/61217005089f77cb9ff45e40ecc912e526e77d25))
* **e4:** expose human-readable labels in get_overview() (GH-157) ([10deb6e](https://github.com/mischuh/canonic/commit/10deb6eb60681af12b50eb91978d5831889f9a8b))
* **e4:** expose human-readable labels in get_overview() (GH-157) ([ee6e459](https://github.com/mischuh/canonic/commit/ee6e459acb4105ee5d8195e456802168f0f554e4))
* **e4:** headless mode, auto-PR, strict contradiction gate (GH-38) ([9ecc139](https://github.com/mischuh/canonic/commit/9ecc139c88a8f8d9fadcbef5390a3c42203181f5))
* **e4:** headless mode, auto-PR, strict contradiction gate (GH-38) ([25c1994](https://github.com/mischuh/canonic/commit/25c1994253c9e7d15733507be4bb5eb5e40c241c))
* **e4:** Spec ([72d3846](https://github.com/mischuh/canonic/commit/72d3846aa8c3adeee10f28a1fcfa5e1fcebfac6c))
* **e4:** wire four-stage ingestion pipeline + canon ingest CLI (GH-37) ([2fcfed5](https://github.com/mischuh/canonic/commit/2fcfed5c6ad69103b1d099d4b823cd322b449045))
* **e4:** wire four-stage ingestion pipeline + canon ingest CLI (GH-37) ([e60d4b1](https://github.com/mischuh/canonic/commit/e60d4b1d39247d0c235a9eebedfddc8ee5bc03d8))
* **e6:** drift, freshness & usage_mode — live rendering, review flags, caveat surfacing (GH-52) ([cf9c96f](https://github.com/mischuh/canonic/commit/cf9c96fc7c3df08864e453490c9e73d54b09d4e6))
* **e6:** drift, freshness & usage_mode (GH-52) ([be6131b](https://github.com/mischuh/canonic/commit/be6131b09b34a62ea7f09ef106457f50f9f72f1f))
* **e6:** graph traversal — expand seed hits over the reference graph (GH-51) ([66e138a](https://github.com/mischuh/canonic/commit/66e138a2c8bd11e69068fe592fd4cc0fdc0e8f6a))
* **e6:** graph traversal — expand seed hits over the reference graph… ([e4d16c2](https://github.com/mischuh/canonic/commit/e4d16c2ada3d95a8a46f700acf95ad4548b8c129))
* **e6:** hybrid BM25 + vector search with RRF fusion (GH-50) ([27d9283](https://github.com/mischuh/canonic/commit/27d9283a9ea10a94417f1428b383eccc0278f7a4))
* **e6:** hybrid BM25 + vector search with RRF fusion (GH-50) ([b0509b2](https://github.com/mischuh/canonic/commit/b0509b2781a365c80c28b3dd47d12f08d116147a))
* **e6:** knowledge page schema — KnowledgePage model + frontmatter loader (GH-46) ([8408ae5](https://github.com/mischuh/canonic/commit/8408ae5cfbefdfb474b5313e7a6346cbd0f064b8))
* **e6:** knowledge page schema (GH-46) ([08312ce](https://github.com/mischuh/canonic/commit/08312cee102e2dd7cb75a672f741600927ad86e9))
* **e6:** reference graph validation — ReferenceValidator + KnowledgeReferenceError (GH-47) ([69ac2ed](https://github.com/mischuh/canonic/commit/69ac2ed25d8396d10aef7cd5200786321f98fe30))
* **e6:** reference graph validation (GH-47) ([0694ddf](https://github.com/mischuh/canonic/commit/0694ddfdde0d3e2acdcca38df7424847093c6929))
* **e6:** scope visibility & strict-additive collision — ScopeResolver (GH-49) ([aa167f7](https://github.com/mischuh/canonic/commit/aa167f7927e687bfba245c4b080dc69cb65a7511))
* **e6:** scope visibility & strict-additive collision (GH-49) ([dfffe54](https://github.com/mischuh/canonic/commit/dfffe549c9a4682d924db1e6f367a49831a01e48))
* **e6:** stale ref pruning (GH-48) ([51008b5](https://github.com/mischuh/canonic/commit/51008b5566f1734f2b3054560565a4bccc8b51ac))
* **e6:** stale ref pruning (GH-48) ([b0a6c35](https://github.com/mischuh/canonic/commit/b0a6c357fdb3590ab1202ab8c4a173d6a03d4b80))
* **e6:** update the example with more use cases ([c771f2d](https://github.com/mischuh/canonic/commit/c771f2df7a7d47be076edea0d7a99d4b835a5b6b))
* enhance dialect support and filter handling in query compilation ([db76adb](https://github.com/mischuh/canonic/commit/db76adbc141153ed9f0ddbfd06a11c08545ad553))
* enhance dimension handling and error messaging in Canon service ([9ed29ca](https://github.com/mischuh/canonic/commit/9ed29cab3e25a4ae589d978f9536c8c128cd1a3d))
* enhance dimension handling and error messaging in Canon service ([a3c8a7d](https://github.com/mischuh/canonic/commit/a3c8a7d683f1c4db54edbcadedf2c14de2957ee7))
* extract parse-level read-only guard (GH-12) ([7b068c4](https://github.com/mischuh/canonic/commit/7b068c4c7b7eacc9202a1b3280b92b3b72618bdc))
* extract parse-level read-only guard into canon/connectors/readonly.py (GH-12) ([8013d86](https://github.com/mischuh/canonic/commit/8013d86631ffac346b2f86fcec31dfa1f21ef257))
* **generation:** add support for parsing JSON wrapped in markdown code fences ([e5b48b6](https://github.com/mischuh/canonic/commit/e5b48b69b29dc5cf31e7286ab9d066a461719f10))
* harden read-only guarantee in dialect adapter (GH-11) ([c0b66c2](https://github.com/mischuh/canonic/commit/c0b66c22a4c8396f5d5c1b1a7f46c91ee9975943))
* harden read-only guarantee in dialect adapter (GH-11) ([10af5d9](https://github.com/mischuh/canonic/commit/10af5d9ab72311031e8c68a20e85a519fd3a2573))
* implement canon setup wizard (GH-15) ([7648ac9](https://github.com/mischuh/canonic/commit/7648ac9a766b4e8a5b927faaae775499a082b507))
* implement canon setup wizard (GH-15) ([710c933](https://github.com/mischuh/canonic/commit/710c933c7e8a893c56ab8f89115f29b7117cd536))
* implement CanonConfig and loader (GH-2) ([57bc914](https://github.com/mischuh/canonic/commit/57bc9146a238ad036e4767f0a349ee171d7c0ff9))
* implement CanonConfig and loader (GH-2) ([3ecf989](https://github.com/mischuh/canonic/commit/3ecf9896c7c066706920d054e0966da422e9a187))
* implement CLI query/sql and complete walking skeleton (GH-14) ([91958d5](https://github.com/mischuh/canonic/commit/91958d5f19fae41ba4ae2e9ea2ba5ee873c3c3d5))
* implement CLI query/sql and complete walking skeleton (GH-14) ([bde38cc](https://github.com/mischuh/canonic/commit/bde38cc794b7d5d1cd5cae3937b98969e0922d2f))
* implement MCP serving surface (GH-13) ([920f074](https://github.com/mischuh/canonic/commit/920f074a68f7d3bc7e700b7b828686e86d0c5866))
* implement MCP serving surface (GH-13) ([dc36227](https://github.com/mischuh/canonic/commit/dc362278e33d270190502a12429536955bfe6d02))
* implement related metadata for unused dimensions and sibling metrics in compile results ([17319db](https://github.com/mischuh/canonic/commit/17319dbb33bd9e3fa7709a1104656f6091dcb326))
* implement SPEC-P0 serving contract interface freeze ([18804b2](https://github.com/mischuh/canonic/commit/18804b2f5cd1480e35feadb94086a8b6f9db3f3b))
* **ingest:** draft dimension labels and aliases during LLM bootstrap ([9fe2c2c](https://github.com/mischuh/canonic/commit/9fe2c2c822e1275a1116ee51e2663e4ee98c2acd))
* **ingest:** draft FK-less joins from column-name convention during bootstrap ([6efb213](https://github.com/mischuh/canonic/commit/6efb2136a3263bf411d260ce67811de33f2b0851))
* **ingestion:** implement ContextBuilder — deterministic core (GH-33) ([5e42d71](https://github.com/mischuh/canonic/commit/5e42d71af27d6fd4b25b7d32d7323927474a00e5))
* **ingestion:** implement ContextBuilder — deterministic core (GH-33) ([7e8c5db](https://github.com/mischuh/canonic/commit/7e8c5db3ce2516cc99b35531e196f1268b2e7ffc))
* **jaffle-shop:** add metrics and semantics for avg_product_price, avg_revenue, num_customers, product_count, total_price, and update provenance for revenue ([aac3081](https://github.com/mischuh/canonic/commit/aac3081da4b443d32245bb8fc82fa7753272c6dc))
* **knowledge:** add one-shot `canonic knowledge add` alongside recurring ingest ([1b8e2a5](https://github.com/mischuh/canonic/commit/1b8e2a5c10a9e320781c239befd11998ff5d9f54))
* **llm:** support Anthropic, OpenAI, and GitHub Copilot as llm providers ([a834d6d](https://github.com/mischuh/canonic/commit/a834d6de9a1c7fe13d76d0916177a0aa5fcdf0b2))
* **llm:** support Anthropic, OpenAI, and GitHub Copilot as llm providers ([70fb0c9](https://github.com/mischuh/canonic/commit/70fb0c9f2e84cb194bcc09ac1827e3842f19f698))
* **logging:** add configurable logging framework ([4156b25](https://github.com/mischuh/canonic/commit/4156b25ad7000c27cc9b7be4108dbec45fd5ec2f))
* **logging:** add query ID context for enhanced log correlation ([85329d1](https://github.com/mischuh/canonic/commit/85329d145bf95b9a755b87b604cd7efe5f2d88e8))
* **logging:** enhance logging configuration by loading from canon.yaml ([910030f](https://github.com/mischuh/canonic/commit/910030f91439edd720cdb7da2fd782dffeda7942))
* **logging:** enhance logging configuration to support JSON format and update related components ([bdd62a9](https://github.com/mischuh/canonic/commit/bdd62a968d717e1cc3c9b12505adb4d376d02b3c))
* **logging:** instrument setup, ingest, review, and apply commands ([7d38da1](https://github.com/mischuh/canonic/commit/7d38da10b69e82d4c3a93a8a2930e559d298a815))
* **logging:** instrument setup, ingest, review, and apply commands ([7c938c9](https://github.com/mischuh/canonic/commit/7c938c9c598addcb007036441314c9aeaa816623))
* **OB-S1:** add --project flag and last-project fallback to mcp start (GH-136) ([ddbc108](https://github.com/mischuh/canonic/commit/ddbc1082983f07b85bcccf477788eb716dd57431))
* **OB-S1:** add --project flag and last-project fallback to mcp start (GH-136) ([3d32fbd](https://github.com/mischuh/canonic/commit/3d32fbd64eb36d3869c49002b2d81d09d763dc6a))
* **OB-S2:** auto-accept deterministic core on bootstrap (GH-137) ([3204a1f](https://github.com/mischuh/canonic/commit/3204a1f81408cf7345cd8676274e9f7e40d7f844))
* **OB-S2:** auto-accept deterministic core on bootstrap; withhold llm-drafted proposals (GH-137) ([c1520e0](https://github.com/mischuh/canonic/commit/c1520e03558d033b7ca621382ecf7d9367975501))
* **OB-S3:** make first-run auto-accept safe and one-time (GH-138) ([95ff7d3](https://github.com/mischuh/canonic/commit/95ff7d38ddd4b2d2dbace124df24c309816ab0f4))
* **OB-S3:** make first-run auto-accept safe and one-time (GH-138) ([d9ab3bd](https://github.com/mischuh/canonic/commit/d9ab3bdc3510347ab7ca4eeb87aed2e7a9ffec02))
* **OB-S4:** curated first review — 3-tier priority, teachable units, deferred count (GH-139) ([7a4fd12](https://github.com/mischuh/canonic/commit/7a4fd1227a53e394403a766754568c773feaa5bf))
* **OB-S4:** curated first review — 3-tier priority, teachable units, deferred count (GH-139) ([2baa397](https://github.com/mischuh/canonic/commit/2baa397ce4ac365efb8f91a8d2430636181666b0))
* **OB-S5:** honest failure modes — surface registry error, describe-level fallback, no-LLM note (GH-140) ([5329695](https://github.com/mischuh/canonic/commit/532969501cd8fd0eb0256cad91c545e1ba81b06e))
* **OB-S5:** honest failure modes — surface registry error, describe-level fallback, no-LLM note (GH-140) ([f49ecc5](https://github.com/mischuh/canonic/commit/f49ecc5d679c6e249c63e1443e3e1a7976a9b7a6))
* **OB-S6:** funnel instrumentation — emit 5 onboarding milestones to E16 log, surface time-to-first-answer in canon report ([4bdee00](https://github.com/mischuh/canonic/commit/4bdee00f7dee3d557a13cfd898f951ea947716cb))
* **OB-S6:** funnel instrumentation (GH-141) ([5014af1](https://github.com/mischuh/canonic/commit/5014af13a5770f9735c1ebaefae09c3a7b2380d5))
* persist ingest diffs to .canon/pending-diffs/&lt;run-id&gt;/ (GH-149) ([7a89a46](https://github.com/mischuh/canonic/commit/7a89a461c17f1ee98d07c4f8bdd5575e835f23b4))
* persist ingest diffs to .canon/pending-diffs/&lt;run-id&gt;/ (GH-149) ([c791b79](https://github.com/mischuh/canonic/commit/c791b791fec0713a48f1cc92e74061f2599cadd7))
* PostgreSQL connector — P0 concrete connector (GH-4) ([015b44f](https://github.com/mischuh/canonic/commit/015b44f7a0c5eff78dacb4d1c02d3e24b960bf35))
* PostgreSQL connector — P0 concrete connector (GH-4) ([1eb31b9](https://github.com/mischuh/canonic/commit/1eb31b9521bfddd0bf93ac8c1ff5a7c5b1a11f31))
* propagate dbt entity grain to DuckDB schemas during bootstrap ([aec41f8](https://github.com/mischuh/canonic/commit/aec41f8d61418ecb3e5b9d87bfae39df540af498))
* **redshift:** enable statement cache support in Redshift dialect ([7c85b5b](https://github.com/mischuh/canonic/commit/7c85b5b3eec00b0e7f41d2e244be2f93791f86fe))
* **release-gate:** add resolve/run_sql parity tests and named CI gate ([155a890](https://github.com/mischuh/canonic/commit/155a890687c5adba11f9844c63adee3c6a607df3))
* **release-gate:** add resolve/run_sql parity tests and named CI gate ([bc090ba](https://github.com/mischuh/canonic/commit/bc090ba3da5c6e1180152d49173fe348d02e575b))
* **release:** add publish workflow for docker, npm scaffold, homebrew TODO ([478aa5e](https://github.com/mischuh/canonic/commit/478aa5e6fc5dec7b529474d3b9abb8bc3320835b))
* **release:** automate version bumps via release-please ([bc0b740](https://github.com/mischuh/canonic/commit/bc0b74055b654a914e636617813276c5104e9f15))
* repo scaffold & toolchain (GH-1): ([ef600aa](https://github.com/mischuh/canonic/commit/ef600aa96d50d2e8b4048eb0042c61427213852e))
* repo scaffold & toolchain (GH-1): ([e45bc2a](https://github.com/mischuh/canonic/commit/e45bc2a8506141db33687ca578cce2a3a8797838))
* **runtime:** strengthen LLM grain-inference prompt with data profiles ([a8b3405](https://github.com/mischuh/canonic/commit/a8b3405257cef32d3fed17f35e2777264e1b6184))
* **runtime:** strengthen LLM grain-inference prompt with data profiles ([d29fc51](https://github.com/mischuh/canonic/commit/d29fc51a74edd6d3a93ea06e9329cbd18034ea91))
* **saas-dwh-example:** Add new data marts and setup script for SaaS analytics ([5d0f441](https://github.com/mischuh/canonic/commit/5d0f441a1abfb5ec40cc185c6e607894155bedbb))
* schema acquisition ladder (tiers 4 & 6) + validation probe (GH-7) ([19e7605](https://github.com/mischuh/canonic/commit/19e760532c029449601f516a8396790cde2a687f))
* semantic source schema — YAML ↔ Pydantic (GH-5) ([06eaa8c](https://github.com/mischuh/canonic/commit/06eaa8ca001df950cb3b5fbb4002eed94a1eff7c))
* semantic source schema — YAML ↔ Pydantic (GH-5) ([703b5df](https://github.com/mischuh/canonic/commit/703b5df0cc9ba03bc7c7f6b1309b482e091e78bd))
* **service:** include ratio and weighted_avg metrics in list_metrics ([7d6c375](https://github.com/mischuh/canonic/commit/7d6c37521ff2af0992597c78912b763bf7916be1))
* **service:** include ratio and weighted_avg metrics in list_metrics ([34dee3a](https://github.com/mischuh/canonic/commit/34dee3a128c674ba264df95797e149a8bdbe45d9))
* **service:** resolve relative file paths in file-based connections ([936e625](https://github.com/mischuh/canonic/commit/936e625e308e4dcddd1be0cd5d5cb049db2691b2))
* set up release automation (release-please, publish, commitlint, contract-schema guard) ([7aa5d20](https://github.com/mischuh/canonic/commit/7aa5d2087cba10f0894a2cd624d458d2b6f99f58))
* **setup:** add Redshift connection prompt and parameters collection ([9429d96](https://github.com/mischuh/canonic/commit/9429d9612a69b70a6165ca919c45ad92fe836e2c))
* **setup:** Add schema/table narrowing to setup, fix garbled review menu ([a618498](https://github.com/mischuh/canonic/commit/a618498cbc8ea02faa63c1fed574b08e9d9924e5))
* **setup:** Add schema/table narrowing to setup, fix garbled review menu ([f20c539](https://github.com/mischuh/canonic/commit/f20c539eedff5ee7b936e51787c21a3eabd4a3fa))
* update contract schema to version 1.5 and propagate dimension labels in unused dimensions ([dbacb2a](https://github.com/mischuh/canonic/commit/dbacb2a692e4efaee69efaeebf5023970d01e443))


### Bug Fixes

* **ci:** rename remaining canon references ripgrep skipped in hidden paths ([631e72b](https://github.com/mischuh/canonic/commit/631e72bcdf4d0394ddae9484d2a7374a7cefcb05))
* **compiler:** resolve dimensions from metric owner before alphabetical scan ([52d13eb](https://github.com/mischuh/canonic/commit/52d13eba31110a4867be65aa3018c184e288683f))
* **compiler:** restrict dimension suggestions to join-reachable sources ([fe7ebb6](https://github.com/mischuh/canonic/commit/fe7ebb6989595be46f6da76e5ce99ae0a041ec50))
* **daemon:** redirect logging to stderr for stdio transport ([076f866](https://github.com/mischuh/canonic/commit/076f866ba56d926e00119ec67d6a53a4988dea77))
* **e6:** ruff format — collapse list comprehension condition in scope.py (GH-49) ([1ac5a1d](https://github.com/mischuh/canonic/commit/1ac5a1dc4d4a7de10d4bd805932c69441f22b99e))
* **E7/E8 2:** knowledge pages are now fully readable ([d96332c](https://github.com/mischuh/canonic/commit/d96332c2b30317622cd653e8c15bef1c3813ffd1))
* follow PEP 257 and add blank lines ([e4efe82](https://github.com/mischuh/canonic/commit/e4efe82b1666b4b87757ff26d8d599515e2ef3e7))
* **generation:** raise StructuredOutputUnsupported for empty content on schema-constrained requests ([49370c4](https://github.com/mischuh/canonic/commit/49370c43bbfd1ea2d73c2816df313e583e85d68b))
* **gitignore:** add .vscode/settings.json and docs/*.md to ignore list ([8cb0162](https://github.com/mischuh/canonic/commit/8cb01624355a4c181d82cb73ea17cfc2a93e420b))
* **jaffle-shop:** remove obsolete YAML files for customers, order_items, orders, products, and stores ([b267cb0](https://github.com/mischuh/canonic/commit/b267cb0c805d1e5da76160fe44977165936fc296))
* **llm:** correct base_url and update model version in canonic.yaml ([63b8a86](https://github.com/mischuh/canonic/commit/63b8a86d99f4bd8a780d8222d098c4d9a3482f3b))
* **llm:** correct base_url and update model version in canonic.yaml ([8b21a62](https://github.com/mischuh/canonic/commit/8b21a624c686b5913d27664c47ef778c6a911e94))
* make linter happy ([f88080d](https://github.com/mischuh/canonic/commit/f88080d115e6f9e03edc132712057f08fe5d91cb))
* make linter happy (GH-108) ([3eec7a9](https://github.com/mischuh/canonic/commit/3eec7a9b29e334b45a54bd5f44a4616b88f63478))
* make linter happy (GH-68) ([04a2a8a](https://github.com/mischuh/canonic/commit/04a2a8ac52f21169d17aab5ecbf14aa9ceb45faf))
* make linter happy (GH-80) ([3190290](https://github.com/mischuh/canonic/commit/3190290d076cd79d5eb2b464df8b7429bb2d8b64))
* make linter happy (GH-87) ([bebefcc](https://github.com/mischuh/canonic/commit/bebefccd491742369c1ecec1b2f1ab6dcf1d891d))
* make pytest happy ([9950282](https://github.com/mischuh/canonic/commit/99502821dc5682130fed58b8058afdb5ce189b2c))
* make pytest happy (GH-67) ([b1c8e3c](https://github.com/mischuh/canonic/commit/b1c8e3cf8fc268a4668f39f9bf6ae0dbfa344983))
* make ruff happy (GH-79) ([902b386](https://github.com/mischuh/canonic/commit/902b386c19587a6403584a1ef5c41239cf4f5051))
* make ruff happy (GH-79) ([f846562](https://github.com/mischuh/canonic/commit/f846562a7bdb32ff100f588b5fd9e62f1634daf8))
* make ruff happy (GH-80) ([7267c1d](https://github.com/mischuh/canonic/commit/7267c1d73e270bf34b81475f348ee892fb7b5139))
* **mcp:** dedupe dimensions in list_metrics into a shared catalog ([6ca00fa](https://github.com/mischuh/canonic/commit/6ca00faebd45d013378c077c8fbcadf7a84a7e60))
* misleading error message - canon ingest ([8f3ccd8](https://github.com/mischuh/canonic/commit/8f3ccd81195a01e833d209ab2cd6afc7da087945))
* **pipeline:** ensure all relevant filters are joined in finality union branches ([2dfafd8](https://github.com/mischuh/canonic/commit/2dfafd8bffb32c80480527130878c5afa607b993))
* **pipeline:** ensure all relevant filters are joined in finality union branches ([efe04fb](https://github.com/mischuh/canonic/commit/efe04fb16a2f276b015d3905eb0a4d83f693d130))
* pytest ([7f341b6](https://github.com/mischuh/canonic/commit/7f341b6db013dc15b68871a4c97880ac54744e7d))
* pytest (GH-67) ([ad4fba9](https://github.com/mischuh/canonic/commit/ad4fba93ac8232e6e9173eea8d313f244c5e2f2f))
* **reconcile:** reconcile baseline now writes a single section ([6ebb5da](https://github.com/mischuh/canonic/commit/6ebb5da9d06f49459d25ab50aa24b1e238aaf01b))
* **redshift:** implement asyncpg dialect for Redshift compatibility ([97f63d5](https://github.com/mischuh/canonic/commit/97f63d510942ef738f1856c82ad547d5dbbc9fdb))
* resolve ruff TC001 in test_adapter_parity (CanonService in TYPE_CHECKING block) ([1be2f0a](https://github.com/mischuh/canonic/commit/1be2f0a33077267a13e23ef17d86b035a9005f81))
* **resolver:** streamline logging for ambiguous metric resolution ([3929947](https://github.com/mischuh/canonic/commit/3929947b2f62dd00f9feb43a8a4cc9a9eaa201cf))
* Rich redering ([a78258d](https://github.com/mischuh/canonic/commit/a78258dbe668f88f0a57d2c755623fba3080d4b7))
* ruff format — add missing blank line after __all__ in base.py ([9009127](https://github.com/mischuh/canonic/commit/90091275b83c8c0051e2062164886bf9a74e8f09))
* **semantic:** reject duplicate source names across connections ([6687e3b](https://github.com/mischuh/canonic/commit/6687e3b4b731b27c4cc8fcd8c10a9d21f8b97e2b))
* **semantic:** reject duplicate source names across connections ([59d4ca0](https://github.com/mischuh/canonic/commit/59d4ca0259b0bc8970c4700d3db3b33f1c705077))
* **service:** extend describe_metric ([d670dfe](https://github.com/mischuh/canonic/commit/d670dfe7b7051b6b230175da1176ca84273fca13))
* **service:** extend describe_metric to support distinct_count and percentile metrics ([4d0f643](https://github.com/mischuh/canonic/commit/4d0f643fa02273b5e0e0438ed9e6cde452dce669))
* **service:** update connection type hint in _resolve_connection_paths function ([86a0798](https://github.com/mischuh/canonic/commit/86a07989c39bceffbdec0028156679815c615371))


### Documentation

* **readme:** document recurring vs. one-shot knowledge ingestion ([4389f53](https://github.com/mischuh/canonic/commit/4389f533fa179eaa170b1458ed14c3a7b7b8c418))
* **server:** enhance instructions for handling definitional and methodology questions ([f97912a](https://github.com/mischuh/canonic/commit/f97912a781bc8a2440d6c32170ae792670b3afdc))
* **server:** enhance instructions for SQL usage and handling suspicious results ([1269f5e](https://github.com/mischuh/canonic/commit/1269f5e38758d8a4b1bd6f05deea9b4d609c06ef))
* update README for serving contract interface freeze ([e10f12d](https://github.com/mischuh/canonic/commit/e10f12d80336874397b1969080f501a9397fdcbb))
