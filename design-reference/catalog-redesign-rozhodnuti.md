# Catalog 2.0 – redesign a další směr vývoje (stav k 6. 11. 2026)

Souhrn rozhodnutí z návrhové diskuse. Slouží jako podklad pro navazující vlákno a pro zápis úkolů do `docs/WORK_PLAN.md` (tam anglicky, s konkrétními hodnotami).

- **Interaktivní návrh (plátno Claude Design):** https://claude.ai/artifact/RDf2Kat7xRSnMpRbtDU2ER – obrazovka „Stránka složky (interaktivní)“, v režimu Play funguje strom, posuvník, rotace náhledů, modal, připínání a stránkování. Vedle ní artboardy stavů stránky (načítání, prázdné a nedostupné, běžící úloha, úzké okno 960 px).
- **Záloha zdroje návrhu:** projektový dokument `claude/design/Main.dc.html`.
- Návrh používá zástupné obrázky a vymyšlené názvy; skutečné chování (kvalita výběru náhledů, výkon) se ověří až na datech.

## 1. Pořadí práce

1. **Výkon (rychlé opravy):**
   - Načíst pyvips/libvips už při startu serveru (dnes se načítá až při prvním náhledu → cca minuta čekání na první dlaždice po spuštění). Patří k úkolu 4.
   - Dovolit prohlížeči ukládat náhledy (dnes `Cache-Control: no-store` → při každém návratu se čte znovu z disku). Úkol 25.
   - README: doporučení mít složku instance (databáze + cache) na SSD u velkých sbírek; média mohou zůstat na HDD. Jen doporučení, ne povinnost.
2. **Refaktoring frontendu před redesignem:**
   - Rozdělit `app.js` (~10 000 řádků) na moduly (API, historie/adresy, stránka složky, mřížka médií, modal, nastavení, úlohy) bez změny chování.
   - Uklidit `style.css` (45 vrstev „Step/Phase“ s přepisovanými pravidly).
   - **Barvy jako pojmenovaná sada (povinné):** dnes `style.css` obsahuje 242 pravidel přepisovaných pro jednotlivá témata a 128 natvrdo zapsaných barev. Převést všechny barvy na sadu proměnných (pozadí, plochy, text, akcent, stavové barvy…); téma = jiná sada hodnot. Nové prvky pak fungují ve všech pěti tématech včetně světlého „Warm Light“ bez ručního dopisování.
   - **Výchozí prvky prohlížeče řídit stejnou sadou:** aktivní pole má jediný rámeček v barvě akcentu (bez druhého rámečku prohlížeče); každé téma nastaví `color-scheme` (tmavé/světlé) a barvy rozbalovacích seznamů (`option`), protože Chrome a Edge kreslí otevřený seznam po svém – dnes `style.css` pravidla pro `option` nemá (příčina nečitelných seznamů z úkolu 5). Úkol 5 zůstává jako ověření v Chrome/Edge.
   - Přidat testy ve skutečném prohlížeči (Playwright, spouštěné ve Windows): otevření složky, počet náhledů, Zpět, F5. Postupně nahradí testy, které jen hledají text v `app.js`.
3. **Redesign po samostatně ověřitelných krocích:**
   1. rozměry médií (šířka/výška) a délka videí při aktualizaci katalogu + větší dlaždice fotek,
   2. mřížka médií v řádcích podle poměru stran + nový modal,
   3. nové zobrazení složek (koláž) + jedno stránkování + strom jako vysouvací panel + nová hlavička,
   4. vyřazení zjevně špatných fotek z náhledů složek + zajímavý ořez,
   5. ruční připínání náhledů složek (na konec posun výřezu u připnutých).
4. **Zbytek plánu:** Nastavení, přejmenování (úkol 22), Chrome/Edge (úkoly 5, 6), závěrečná regrese (úkol 13).

Vývoj a ladění na **malé vzorové instanci**; celá sbírka až na závěr jako zkouška výkonu. Kvůli změnám schématu a dlaždic se instance vytvoří **načisto** (pravidlo projektu: žádné přechodové vrstvy před 1.0).

## 2. Rozložení stránky

- **Šířka obsahu:** max. 1600 px, na střed, okraje po stranách. Jedna hodnota v CSS, aby šla doladit. Hustotu si uživatel může upravit i zvětšením prohlížeče (Ctrl + / −).
- **Hlavička:**
  - vlevo ☰ (strom) a logo „Catalog 2.0“ – 40 px, tučně, v akcentové barvě tématu (přejmenovatelné); pod ním malým písmem odkaz GitHub,
  - uprostřed vyhledávací pole jako hlavní prvek; „Jen aktuální složka“ jako ikona složky uvnitř pole (přepínač, zvýrazněný akcentem), hledání lupou v poli nebo Enterem; samostatné tlačítko Hledat zrušeno,
  - vpravo tlačítka Oblíbené (navigace do pohledu) a Nastavení (text); tlačítka v hlavičce jsou větší (40 px) než akce v obsahu,
  - stav úloh: když nic neběží, nezobrazuje se; když úloha běží, štítek podle oddílu 11.
- **Akcentová barva** jen pro logo a aktivní stavy (aktivní filtr, vybraná položka, přepínače), ne pro běžná tlačítka.
- **Strom:** vysouvací panel pod ikonou ☰ přes obsah (stránka se neposouvá). Obsahuje pole „Najít složku“, sekce Oblíbené složky, Naposledy navštívené a strom bez ikon s počty položek. Nastavení zůstává samostatný modal (jiný typ úkolu – to je v pořádku).
- **Záhlaví složky:** drobečková navigace; název (30 px) a vedle něj ikony bez rámečků: tužka (přejmenovat), hvězdička (přidat do oblíbených), ⋯ (další akce: otevřít ve Windows, aktualizovat, přepočítat náhledy). Pod názvem jeden řádek nenulových počtů.
- **Řádek filtrů:** Vše / Složky / Fotky / GIFy / Videa / Ostatní jako podtržené záložky; vpravo kompaktní stránkování „‹ 3 / 10 ›“ (klik na číslo → pole pro skok, Enter potvrdí, Esc zruší). Při jedné stránce se nezobrazuje.

## 3. Složky

- **Koláž v mřížce:** karty v mřížce `repeat(auto-fill, minmax(340px, 1fr))` (≈4 v řádku při 1920 px, více na 2K). Každá karta: 1 velký náhled + 4 malé (mřížka 2fr 1fr 1fr / 2 řádky, poměr karty 4:3), pod ní název a počty. Hvězdička a tužka u karty se ukážou po najetí myší.
- **Všechny náhledy složek oříznuté na jednotný poměr na výšku** (jednotný vzhled všech složek, pevná výška). Média se naopak neořezávají.
- **Počet náhledů na složku: 9** (konstanta `FOLDER_PREVIEW_REQUESTED_COUNT` 12 → 9): místo 1 hlavní, 2–5 vedlejší (4 malé), 6–9 jen v rotaci.
- **Rotace:** jen hlavní náhled. Pohyb myši zleva doprava po velkém náhledu prochází všech 9 (ne časovaně); pruh s 9 dílky dole ukazuje pozici. Najetí na malý náhled ho ukáže ve velkém rámečku. Malé náhledy se nemění. Zbylé náhledy se načítají až při najetí myší.
- **Vyřazení zjevně špatných fotek (schváleno 6. 11., varianta C):** 9 fotek se vybírá rovnoměrně po složce jako dnes. Z dlaždic, které pro kartu vznikají stejně, se spočítá jednoduché pevné pravidlo „zjevně špatná fotka“ (téměř jednobarevná – zeď, strop, zakrytý objektiv; téměř černá; téměř bílá). Takovou fotku nahradí sousední fotka ze stejné složky; když je špatná i ta, další soused, nejvýš do pevného limitu na složku. Pravidlo je stejné pro všechny uživatele, nastavené opatrně jen na zjevné případy; uživatel nic nenastavuje. Cena: 9 čtení na složku + jen náhrady. (Zamítnuto: výběr nejlepších z 27 kandidátů – asi 3× delší první příprava; předpočítání z vložených EXIF náhledů – závisí na tom, zda je soubory mají.)
- **Zajímavý ořez pro automatická místa:** při tvorbě dlaždice určit střed ořezu pomocí pyvips `smartcrop` (`attention`) a použít ho místo středu (`object-position`). Pár milisekund na fotku, nic se nečte navíc.
- **Nadřazené složky:** dělení míst mezi podsložky zůstává podle velikosti (`_weighted_select_rows_by_source`, odmocnina z počtu médií); v rámci podílu podsložky mají přednost její ručně připnuté fotky.

## 4. Ruční připínání náhledů

- **Ruční posun výřezu u připnutých míst (schváleno 6. 11., v interaktivním návrhu):** v panelu připnutí se připnutá fotka v místě posouvá tažením myší (rámeček zůstává, posouvá se obrázek), dvojklik vrátí střed; výřez se uloží a karta se rovnou změní. Jen v panelu, ne na kartách (riziko omylu). Technicky: dva sloupce se středem výřezu u ručních míst v `folder_preview_items`, použití přes CSS `object-position` – náhledy ani cache se nepřegenerují. Zařadit jako poslední část kroku 5.
- V modalu ikona špendlíku „Připnout do náhledu složky“ → vysouvací panel ukotvený pod tlačítkem: výběr úrovně (vlastní složka nebo některá nadřazená) a plánek 9 míst (hlavní, 4 vedlejší, 4 v rotaci). Klik na místo připne aktuální médium, opětovný klik místo uvolní. Připnutá místa jsou označená.
- **Pravidla:**
  1. automatický přepočet (aktualizace katalogu, nová alba, příprava náhledů) nikdy nemění připnutá místa, doplňuje jen nepřipnutá (bez duplicit),
  2. připnutí ruší jen uživatel: u místa nebo „Obnovit automatický výběr…“ pro složku – nenápadný odkaz s potvrzením v panelu („Uvolnit připnutá místa (N) ve složce X?“),
  3. když připnuté médium zmizí, místo se vrátí do automatického režimu (náhled nikdy nezůstane prázdný),
  4. připnutí/uvolnění rovnou přepočítá danou složku a její nadřazené; uživatel nespouští přepočty ručně.
- **Podoba panelu (upraveno 6. 11.):** řada „V rotaci“ (místa 6–9) přes celou šířku koláže, dlaždice stejného tvaru jako malé náhledy; připnuté místo všude označené tečkou v barvě akcentu (i hlavní); text „Kliknutím na místo fotku připnete, dalším kliknutím ji uvolníte.“ Čísla míst zkoušena a zamítnuta (působila násilně).
- Databáze s tím počítá (`folder_preview_items`, typ `manual`, pozice). Ověřit: zachování vazby po přejmenování souboru, ochrana dlaždic ručních míst v chráněné cache (úkol 9), skládání výsledného náhledu z ručních + automatických míst.

## 5. Média

- **Řádky podle poměru stran:** všechna média v řádku mají stejnou výšku, šířka podle skutečného poměru originálu (rozměry z databáze), bez ořezu; výška řádku se mírně upraví, aby řádek vyplnil šířku. Poslední řádek se nenafukuje.
- **Extrémní poměry (schváleno 6. 11.): 1:2,5 až 3:1.** Na výšku pokryje i snímky obrazovky z telefonu (~1:2,2); panoramata (4:1–8:1) se v mřížce vystředěně oříznou na 3:1 (při výšce 260 px asi 780 px, polovina řádku). V modalu se fotka vždy ukáže celá. Jen pravidlo rozložení, bez dopadu na generování.
- **Bez rámečků a textu**; po najetí myší hvězdička vpravo nahoře a název souboru dole. Video: ikona přehrávání uprostřed (kreslí ji Catalog, platí pro všechny formáty včetně AVI). GIF: štítek „GIF“.
- **Posuvník velikosti** v záhlaví sekce Média, mění jen média (složky mají pevnou podobu). Škála výšky řádku **schválená 6. 11.**: 180 / 220 / **260** / 310 / 360 px, výchozí 260 (vyzkoušeno v interaktivním návrhu, srovnání v artboardu „Posuvník velikosti médií – škála“).

## 6. Stránkování

- **Jeden seznam: nejdřív podsložky, pak média**, jedno stránkování, v adrese jediný parametr `page` (nahradí `folder_page` a `media_page`). Počet položek na stránku viz oddíl 7b (jedna hodnota 24/48/96/192, výchozí 48).
- Tlačítko pro sbalení podsložek se ruší (nahrazují ho filtry).
- Nahoře kompaktní stránkování v řádku filtrů, dole plné (« ‹ [3] / 10 › »). Klávesové zkratky pro předchozí/další stránku (např. Page Up / Page Down) – zapsat do zadání.

## 7. Modal média

- Přes celé okno nad stránkou; po zavření zpět na stejné místo.
- **Fotka se otevírá přizpůsobená velikosti okna.** Zoom: − [%] + (klik na procenta = přizpůsobit oknu), ikona 1:1 = originální velikost.
- **Listování jen šipkami** po stranách s plochou pro kliknutí přes celou výšku okraje (96 px). Klik na fotku nic nedělá (řeší i modré bliknutí, úkol 30).
- **Zavření:** křížek, Esc a klik na tmavou plochu kolem fotky (ne na fotku, šipky ani lištu). Uživatel to zatím vnímá jako trochu matoucí – posoudit ve funkční verzi.
- **Lišta jen z ikon s popisky po najetí**, pořadí zleva: oblíbené (hvězdička), připnout (špendlík) | otevřít originál ve výchozí aplikaci (↗; u snímku z videa „Otevřít video“), otevřít zdrojovou složku (ikona složky) | − % + 1:1 | zavřít. „Otevřít originál“ otevírá soubor v systémové výchozí aplikaci (`/api/open-original`), ne v prohlížeči.
- **Přejmenování přímo v liště:** tužka vedle názvu změní název na pole, přípona zvlášť (nepřepisuje se), Enter uloží, Esc zruší, duplicita se hlásí pod polem. Bez samostatného dialogu. Musí jít změnit i jen velikost písmen (dnes chyba, úkol 22).
- Pruh náhledů v modalu se nepoužívá.
- **Video v modalu (schváleno 6. 11., v interaktivním návrhu):**
  - Šipky listují fotky i videa v jedné řadě v pořadí mřížky (dnes modal s videem listování přerušuje – `openVideoModal` maže `imageItems`); při odchodu se video zastaví.
  - Video, které prohlížeč přehraje (`is_html_playable`, `media_types.py`): vestavěné ovládání prohlížeče, spustí se samo se zvukem (klik je akce uživatele), ztlumení a hlasitost se pamatují.
  - Video, které prohlížeč nepřehraje (např. AVI): modal se otevře také (dnes klik rovnou spouští systémový přehrávač), velký náhled z videa, věta „Tento formát nejde přehrát v prohlížeči.“ a hlavní tlačítko „Přehrát v přehrávači“ (`/api/open-original`).
  - Klávesy: ←/→ vždy listují, mezerník přehrát/pozastavit; modal drží fokus, aby šipky nepřeskakovaly ve videu.
  - Lišta jako u fotky bez zoomu (−, %, +, 1:1); video se přizpůsobí oknu, celá obrazovka přes ovládání přehrávače. ↗ má u videa popisek „Otevřít v přehrávači“.
  - Zavírání stejné; klik do videa nebo na ovládání modal nezavře.
  - Délka videa: číst `ffprobe` spolu s rozměry v kroku 3.1 a ukazovat v informačním řádku.

## 7a. Výsledky hledání a Oblíbené (schváleno 6. 11., artboard „Výsledky hledání“)

- **Dnešní stav (ověřeno):** `search_page` porovnává text s celou cestou (`path_key LIKE`, tedy názvy složek i souborů) a s typem média; řazení podle cesty; filtry Vše/Složky/Fotky…; omezení na složku; zvlášť stránkování složek a médií. Oblíbené (`favorites.json`, s datem přidání) mají stejnou strukturu.
- **Šablona jako stránka složky:** hlavička, filtry, jedno stránkování, koláže složek. Místo drobečkové navigace záhlaví „Výsledky pro „{text}““ + štítek rozsahu („v celém katalogu“ / „ve složce {název} ✕“ – křížek rozšíří hledání na celý katalog) + řádek počtů.
- **Nalezené složky** jako karty koláže; shoda v názvu podtržená akcentem, pod názvem nadřazená cesta.
- **Nalezená média seskupená podle složky:** každá složka se shodou = jedna skupina: řádek s cestou (odkaz do složky), počet nalezených, „Zobrazit vše →“ a **jedna řada ukázek** (oříznutá na šířku). **Stránkuje se po skupinách** (např. 20 na stránku), ne po fotkách – nutné kvůli velkým sbírkám (hledání může najít 100 000 fotek).
- **„Zobrazit vše →“** otevře hledání omezené na tu složku (existující omezení rozsahu) s běžným stránkováním všech nalezených médií.
- **Zvýraznění shody:** je-li shoda v cestě, podtržení v řádku cesty; je-li v názvu souboru, cesta bez podtržení a shoda podtržená v názvu, který se ukáže po najetí myší.
- **Pořadí podle cesty** (předvídatelné, bez změny řazení).
- **Modal z výsledků** listuje ve výsledcích; v informačním řádku celá cesta a odkaz „Ukázat ve složce“ (kotvy `/api/search/anchor`, `/api/favorites/anchor` existují).
- **Prázdný výsledek:** „Nic nenalezeno pro „{text}““; při omezení na složku odkaz „Hledat v celém katalogu“.
- **Oblíbené beze změny vzhledu** (jeden seznam složek a médií bez seskupování); prázdné: „Zatím nemáte žádné oblíbené. Přidáte je hvězdičkou u složky nebo fotky.“; nedostupná položka zůstává se štítkem „Nedostupné“ a jde odebrat hvězdičkou.
- **Backend:** seskupený dotaz (složky se shodou + počty + prvních N ukázek na složku, stránkování po skupinách) je nový – SQLite to umí (GROUP BY, okenní funkce); viz oddíl 13.

## 7b. Nastavení (schváleno 6. 11., artboard „Nastavení (interaktivní)“)

- Samostatný modal pevné velikosti, vlevo čtyři sekce, vždy se zobrazuje jedna: **Obecné**, **Aktualizace**, **Zdrojová složka**, **Náhledy a cache**. Statistiky cache se počítají až při otevření své sekce (řeší pomalé otevírání, úkol 20). Obecné se ve výchozím stavu vejdou bez posouvání.
- **Jednotný vzor bloků:** nadpis, nejvýš jedna krátká věta popisu (šířka jako pole), ovládání; pole lícují (název katalogu široký jako Jazyk + Položek na stránku vedle sebe).
- **Ukládání:** bezpečné změny se ukládají hned (téma, jazyk, počet položek; název po opuštění pole nebo Enter) s krátkým „✓ Uloženo“ u nadpisu. Změny s dopadem potvrzuje „Uložit“: zdrojová složka a limit cache.
- **Obecné:** název katalogu; Vzhled – pět témat + „Vlastní…“ v mřížce po třech; Jazyk; **Položek na stránku** – jedna hodnota pro všechny filtry, volby **24 / 48 / 96 / 192** (násobky 12 → karty složek vždy v plných řádcích při 1–4 sloupcích), výchozí 48. Nahrazuje dnešní počty pro každý filtr i pevných 48 z oddílu 6.
- **Vlastní barvy (nástřel, doladí se nebo vypustí při implementaci; implementace až po redesignu):** editor čtyř základních barev (pozadí, plochy, text, akcent) s živým náhledem, upozorněním na špatnou čitelnost (kontrast textu a pozadí pod 4,5:1) a „Obnovit výchozí“. Ostatní barvy sady (ohraničení, tlumený text, …) se dopočítají; stavové barvy (úspěch, upozornění, chyba, informace) zůstávají pevné. Předpoklad: refaktoring převede barvy na sadu proměnných (dnes jen 7 barevných proměnných, zbytek natvrdo nebo přepisy pro témata).
- **Aktualizace:** „Aktualizace katalogu“ (popis: najde nové, změněné a smazané soubory, zdrojové soubory se nemění; čas poslední aktualizace; hlavní tlačítko v barvě akcentu) a „Náhledy katalogu“ (Přepočítat náhledy katalogu).
- **Zdrojová složka:** popis „Složka s vašimi fotkami a videi.“; cesta, Ověřit, Uložit (aktivní až po úspěšném ověření, po změně cesty znovu ověřit); po ověření „✓ Složka je dostupná a odpovídá katalogu.“ **Po uložení se nic dalšího nespouští:** `source_root_setting_verify` kromě dostupnosti porovná vzorek složek katalogu s novou cestou a při nízké shodě změnu nepovolí (`source_root.verify.match.low_match`), takže jde vždy o tytéž soubory. Cíl odkazu „Změnit v Nastavení“ z upozornění na nedostupný zdroj.
- **Náhledy a cache:** využití běžné cache s pruhem a chráněné náhledy; limit běžných náhledů v GB + Uložit (popis: kolik místa mohou běžné náhledy zabrat, chráněné se nepočítají); **Úklid** – „Vyčistit nepoužívané náhledy“ spustí za sebou úklid běžné cache (`dynamic-cleanup`) a kontrolu a úklid osiřelých chráněných náhledů (`protected-orphan-check/clean`); maže jen náhledy, na které nic v katalogu neodkazuje.
- **Odebrat z UI:** velikost dlaždic (nahrazuje posuvník u médií), parametry náhledů (zůstávají jen v `config.json` – změna rozbíjí rozložení a vyžaduje přegenerování cache), Servis náhledů, Diagnostika, Servisní výpis (testovací zbytky, úkol 20; tytéž akce zůstávají v `cli.py`: `scan-preview`, `scan-stage`, `scan-activate`, `update-branch`…). Přepočet náhledů jedné složky je v nabídce ⋯ u složky.

## 7c. Přejmenování složky a souboru (schváleno 6. 11., v interaktivním návrhu; úkol 22)

- **Dnešní stav (ověřeno):** samostatný dialog s vývojářským postupem (Ověřit změnu → oddíly Dopad, Cesty, Technické kontroly, Blokující chyby, Varování → Přejmenovat). Přejmenovává se skutečná složka nebo soubor na disku a katalog upraví cesty celé větve, Oblíbené i náhledy.
- **Přejmenování na místě, bez dialogu:** tužka vedle názvu složky v záhlaví změní nadpis na pole stejné velikosti; tužka u karty složky změní název pod koláží na pole; u souboru totéž v liště modalu (přípona zvlášť, oddíl 7). Vedle pole ikony ✓ (uložit) a ✕ (zrušit); Enter uloží, Esc zruší. Během přejmenování karty se skryjí její hvězdička a tužka.
- **Ověření a provedení samo:** dvoukrokový postup backendu (`plan`, `execute`) zůstává, ale uživatel ho nevidí – Enter spustí ověření a při úspěchu rovnou přejmenuje.
- **Chyby jednou větou pod polem**, technické podrobnosti se nezobrazují: „Složka s tímto názvem už existuje.“, „Název nesmí obsahovat znaky \ / : * ? " < > |“ a „Název nesmí končit tečkou ani mezerou.“ (hlídají se už při psaní), „Název nesmí být prázdný.“, „Složku nejde přejmenovat, protože je otevřená v jiném programu.“, „Zdrojová složka není dostupná.“
- **Změna jen velikosti písmen musí projít** (foto → Foto) – chyba z úkolu 22 (Windows názvy lišící se jen velikostí písmen považuje za stejné).
- **Bez přenačtení stránky:** nadpis, drobečková navigace a adresa se změní na místě, u názvu krátce „✓ Přejmenováno“. Během operace (u velkých složek může trvat) pole ukazuje „Přejmenovávám…“ a nedá se do něj psát.
- **Nedostupná složka** (nedostupný zdroj, složka zmizelá z disku) tužku nemá.

## 7d. Instalace, spuštění, rozcestník a odolnost (schváleno 6. 11.; úkoly 4, 18, 23 + nový úkol „Odolnost katalogu“)

**Program a data odděleně (nahrazuje dnešní `setup-instance`):** dnes vzniká vedle zdrojové složky složka `Catalog2_<název>` s vlastní kopií programu (`.venv`, `app`) a `Start Catalog.bat`; každý katalog se aktualizuje zvlášť. Nově:
- **Program jen jednou:** přenosný ZIP (součást 1.0), rozbalí se kamkoli, kde uživatel smí zapisovat (plocha, `D:\Programy`, USB disk; ne `Program Files`). Obsahuje `Catalog.exe` a podsložku s programem včetně Pythonu a libvips (s texty licencí a odkazem na zdrojový kód libvips; libvips zůstává samostatná vyměnitelná knihovna). Bez příkazové řádky, bez instalace Pythonu.
- **Katalogy jsou jen data** (databáze, náhledy, nastavení) s názvem od uživatele, bez technické předpony. Výchozí umístění **vedle programu** (`Catalog 2.0\Katalogy\<název>`) → celé jde přenést na USB disku; „Změnit…“ pro jiné místo. Seznam katalogů si program vede u sebe.
- **Aktualizace programu** = rozbalit novou verzi přes složku programu; `Katalogy` zůstávají netknuté a fungují dál.
- **FFmpeg se nepřibaluje:** s vysvětlením a souhlasem se stáhne z oficiálního zdroje a ověří kontrolním součtem (odpadá otázka licence přísnější verze FFmpeg).
- **Licence Catalogu: MIT** (soubor `LICENSE`, zmínka v README). ZIP se přikládá k vydání v části „Releases“ na GitHubu; zdrojový kód se dál vyvíjí v repozitáři.
- **Jen Windows pro 1.0** (README, `CLAUDE.md`); jiné systémy případně později samostatnými balíčky – kód má části psané pro Windows (spouštěč, „Otevřít ve Windows“).

**Spuštění:** server naběhne okamžitě a prohlížeč hned ukáže stránku „Catalog se spouští…“ (artboard „Spouštění“), která sama přepne dál, až je vše připravené (úkol 4). Okno příkazové řádky uživatel nevidí (nahrazuje původní cíl úkolu 18). Jeden katalog → otevře se rovnou; více → rozcestník; žádný → průvodce.

**Průvodce novým katalogem** (artboard „Průvodce prvním spuštěním“), čtyři kroky:
1. **Nový katalog:** název, umístění (předvyplněné vedle programu, „Změnit…“), poznámka „Běžné náhledy jsou omezené na 20 GB, limit změníte později v Nastavení“ (výchozí `thumbnail_cache_limit_gb` = 20).
2. **Zdrojová složka:** „Procházet…“ otevře okno Windows pro výběr složky (prohlížeč skutečnou cestu neprozradí, proto ho otevře místní server); ruční zápis jako záloha; ověření.
3. **Videa:** stažení FFmpeg se souhlasem, nebo „Teď ne“ (fotky fungují, videa bez náhledů).
4. **První načtení:** počítadlo „Prochází soubory…“, po dokončení souhrn, zaškrtávátko „Vytvořit zástupce „Catalog 2.0“ na ploše“ a „Otevřít katalog“.

**Rozcestník** (artboard „Rozcestník – výběr katalogu (interaktivní)“): seznam katalogů (malá koláž, název, počty, kdy naposledy otevřen, zdrojová složka); klik na řádek otevře katalog. Na stránce „+ Nový katalog“ (průvodce) a „Přidat existující…“ (vybrat složku katalogu, např. z USB nebo po přesunu). Nabídka ⋯ u katalogu: Přejmenovat, Otevřít složku katalogu, **Vytvořit zástupce na ploše** (zástupce otevře přímo tento katalog), Odebrat… → dialog: „Jen odebrat ze seznamu“ (data zůstanou, jde přidat zpět) nebo „Smazat katalog“ (data a náhledy; fotky nikdy).

**Vzhled a jazyk úvodních obrazovek:** spouštění, rozcestník a průvodce mají **vždy výchozí téma** (Classic Orange) – stálá tvář aplikace; vlastní téma má až otevřený katalog (změna barev = signál „jsem v katalogu X“). **Výchozí jazyk angličtina**; rozbalovací výběr jazyka vpravo nahoře na rozcestníku i v průvodci (ikona zeměkoule + název aktuálního jazyka + šipka; seznam jazyků, každý napsaný ve svém jazyce – funguje pro libovolný počet jazyků) (uživatel musí rozumět už instalaci a výběru); dál platí jazyk zvolený v katalogu.

**Ukončení:** otevřená stránka Catalogu průběžně dává serveru vědět, že je otevřená; když žádná není otevřená asi 15 minut, server se sám ukončí – pokud zrovna neběží úloha (pak počká). Zavření karty křížkem tak stačí. Doplňkově ikona v oznamovací oblasti Windows („Otevřít Catalog“, „Ukončit“) a „Ukončit Catalog“ v Nastavení.

**Druhé spuštění a více instancí** jsou vyřešené ve spouštěči (`launcher.py`): běžící instanci jen otevře, jinak vezme nastavený port nebo další volný. (Vývojový příkaz `serve` – `run_server` – to záměrně nedělá.)

**Změna písmene disku:** je-li zdrojová složka na stejném disku jako katalog (typicky USB), katalog si pamatuje cestu vůči svému disku; na jiném počítači zkusí stejnou cestu na disku, kde sám leží, ověří ji kontrolou shody a při úspěchu pokračuje bez ptaní. Jinak upozornění „Zdrojová složka není dostupná“ + „Změnit v Nastavení“.

**Nový úkol „Odolnost katalogu“** (situace mimo Catalog):
- **Složka katalogu zmizela** (smazaná nebo přesunutá v Průzkumníkovi): na rozcestníku přerušovaný rámeček, štítek „Nenalezen“ / „Not found“, text „Složka katalogu byla přesunuta nebo smazána“, tlačítko „Najít…“ a v nabídce „Jen odebrat ze seznamu“ (v návrhu).
- **Poškozená databáze** (např. výpadek proudu): srozumitelná chyba a „Vytvořit katalog znovu“ – fotky se načtou znovu ze zdroje; aby se neztratily Oblíbené, připnutí a nastavení, **automatická záloha těchto drobností** (např. při každém spuštění, několik posledních kopií).
- **Plný disk s katalogem:** chybějící náhledy jako dlaždice „Náhled není k dispozici“; aktualizace skončí chybou „Na disku s katalogem došlo místo.“ **Ověřit**, že díky přípravné fázi aktualizace zůstane původní katalog nedotčený.

## 8. Technická rozhodnutí

- **Dlaždice fotek větší:** dnes max. 300 × 400 px (`image_thumb_size`), fotka na šířku má jen 300 × 200 → v nových řádcích i v koláži by byla rozmazaná. Nově pevná výška **560 px** (schváleno 6. 11.) s omezením šířky (kvůli panoramatům): největší řádek médií má 360 px a při zvětšení obrazovky 150 % (běžné u notebooků a 2K) potřebuje 540 px. Oproti 500 px asi o čtvrtinu víc místa na dlaždici; čas tvorby se téměř nemění (rozhoduje čtení a dekódování originálu). Video postery (`video_preview_width` 1024 → 1024 × 576) stačí bez změny. Cena: 2–3× víc místa na dlaždici a jednorázové přegenerování. Přesnou velikost a odhad cache ověřit na vzorku.
- **Rozměry médií** číst při aktualizaci katalogu (jen hlavička souboru, u videí ffprobe). Poprvé u velké sbírky na HDD znatelný čas (řádově desítky minut až hodiny), dál jen nové/změněné soubory.
- Aktualizace katalogu dlaždice fotek negeneruje (vznikají při procházení a pro náhledy složek), takže větší dlaždice ji zpomalí jen nepřímo.
- **Překlady:** každý nový nebo změněný text (Připnout, popisky ikon, potvrzení, stavy) potřebuje klíče v `cs.json` i `en.json`.

## 9. Předání do Claude Code

- Rozhodnutí zapsat do `docs/WORK_PLAN.md` anglicky s konkrétními hodnotami (úkoly).
- Trvalá pravidla platná pro všechny budoucí úkoly zapsat do `CLAUDE.md`: vykání a pravidla pro texty (oddíl 12), princip „nic neposkakuje“ (místo pro obsah rezervované předem, oznámení během prohlížení plovoucí).
- Tento dokument uložit i do Git-ignorované složky `design-reference/` jako podklad.
- Referenční snímky (1920 a 2560 px) a samostatný HTML náhled uložit do složky projektu, kterou Git ignoruje (např. `design-reference/`); Claude Code umí číst obrázky i HTML. Do repozitáře se nic z toho nedostane.

## 10. Otevřené body

- Nápad na později: uživatelské barevné schéma (editor sady barev z refaktoringu, s kontrolou čitelnosti a „Obnovit výchozí“).
- Ověřit nové prvky ve světlém tématu „Warm Light“ (plátno ukazuje jen tmavé).
- Ve funkční verzi posoudit: ikony místo textů v hlavičce/akcích, zavírání modalu klikem na plochu, velikost ovládání rotace.

## 11. Stavy stránky (artboardy „Stavy – …“ na plátně)

Ověřeno proti kódu (commit 087b71f): `RuntimeJob` (`catalog_app/jobs.py`) posílá jen druh, rozsah, stav, `started_at`, `finished_at`, chybu a výsledek na konci; `JobManager._ensure_start_allowed` povolí jen jednu úlohu; „cancel“ existuje jen u rozhodnutí o chybějících souborech při aktivaci skenu.

- **Načítání (úkol 4):** záhlaví složky, počty a filtry se vykreslí hned z API; dlaždice jako kostry v rozměrech z databáze (bez poskakování). Tenký neurčitý pruh pod hlavičkou až po 300 ms. Načtená dlaždice naběhne za 150 ms; při `prefers-reduced-motion` bez animací. Chyba dlaždice: šedá plocha s ikonou a názvem, médium jde otevřít. Chyba API: hláška místo mřížky s „Zkusit znovu“. Studený start: štítek „Připravuji náhledy…“ na místě stavu úloh, jen do ohlášení připravenosti.
- **Prázdné stavy:**
  1. složka bez podsložek i médií: „Složka je prázdná“ + odkaz do nadřazené složky,
  2. filtr bez výsledků: „V této složce nejsou žádné GIFy“ + „Zobrazit vše“; záložky s nulovým počtem jsou zašedlé (ne skryté), takže tento stav nastane jen z adresy nebo historie,
  3. prázdný katalog nové instance: „Katalog je prázdný“ + tlačítko „Aktualizovat katalog“,
  4. složka už není v katalogu: podle úkolu 19 hlavní stránka s hlášením „Složka z adresy nebyla nalezena: …“.
- **Nedostupný zdroj (není prázdný stav):** jedna zdrojová složka (`data_root`) neexistuje, není složkou nebo ji nejde otevřít (`source_root_status` v scanner.py; ověřuje se při každém načtení, nic se neukládá). Příčin je víc (USB disk, síťový disk/NAS, přejmenování, oprávnění, jiné písmeno jednotky), proto se příčina nehádá. Jedno upozornění pro celou aplikaci nad drobečkovou navigací (jen při načtení stránky): „Zdrojová složka **{cesta}** není dostupná. Zkontrolujte, že je disk nebo síťové umístění připojené.“ + „Obsah se zobrazuje z katalogu; originály teď nejde otevřít.“ Akce **Zkusit znovu** (znovu načte stránku) a **Změnit v Nastavení** (změna cesty s ověřením `source_root_setting_verify`; pro případ jiného písmena jednotky). U složky štítek „Nedostupné“.
- **Složka zmizela z disku (zdroj dostupný):** backend rozlišuje `missing`, `not_directory`, `link_or_junction`. Upozornění jen u složky: „Složka už na disku neexistuje. Mohla být smazána nebo přesunuta mimo Catalog.“ + tlačítko **Aktualizovat tuto složku**; štítek „Nedostupné“, obsah z katalogu zůstává.
- **Běžící úloha – první verze:** štítek „Aktualizace katalogu běží 4:12“ (doba běhu počítaná v prohlížeči ze `started_at`); klik → panel s rozsahem, časem spuštění a dobou běhu. Kolidující akce zakázané s vysvětlením „Už běží jiná úloha: …“. Po dokončení štítek „… dokončena“ na 4 s a plovoucí oznámení dole uprostřed „Katalog se změnil… Obnovit“ (nic neposouvá; nahrazuje dnešní automatické `refreshCurrentViewAndTree` po aktualizaci – změna jen ve frontendu). Hlavička se nezalamuje, aby štítek nic neposunul. Data: `/api/jobs/status` (`current_job`), frontend se ptá každou sekundu – první verze nepotřebuje změnu backendu. K ověření: zda by počítadlo souborů šlo už z `staged` počtů ve stavu skenu (sken zapisuje po dávkách 1000). „Připravuji náhledy…“ potřebuje signál připravenosti (úkol 4), kostry v přesných rozměrech rozměry v databázi (krok 3.1). Chyba: štítek zůstane, dokud ho uživatel neotevře. Čtečce se oznamuje jen začátek a konec. Jen jedna úloha, bez tlačítka Zastavit.
- **Běžící úloha – cílový stav = samostatný úkol pro backend:** průběžný stav úlohy (fáze, hotovo / celkem, čas poslední změny) → procenta v štítku, počítadlo u fází bez známého konce, odhad zbývajícího času až po ustálení rychlosti.
- **Další samostatný backendový úkol:** bezpečné přerušení dlouhých úloh (tlačítko Zastavit). Do první verze se nedává.
- **Úzké okno:** cílový případ je polovina monitoru (960 px u Full HD). Pod 1280 px Oblíbené a Nastavení jako ikony s popisky; pod 900 px vyhledávání pod ikonou lupy (rozbalí se přes hlavičku), filtry vodorovně posuvné. Karty složek `minmax(min(340px, 100%), 1fr)` (960 px → 2 sloupce). Výška řádku médií nejvýš (šířka obsahu − mezera) / 2,5. V modalu se lišta zalomí a plocha šipek se zmenší na 56 px. Garantovaná podpora od 768 px.
- **Výsledky ověření v kódu (6. 11.):** zašedlé záložky jdou beze změny backendu (`folder_detail` vrací počty podle typu, přímé i rekurzivní); rozměry originálů v databázi nejsou (`width`/`height` jen v `thumbnails` a `video_previews`) → kostry podle poměru stran a nová mřížka médií až po kroku 3.1; počítadlo souborů během aktualizace jde už teď (WAL, zápis po dávkách 1000, `latest_scan.staged` v `/api/jobs/status`), dotazovat se po ~5 s, procenta tím nevzniknou.
- **Návrh obsahuje jen to, co uvidí uživatel;** vysvětlivky jsou tady, ne v artboardech. Ukázky „Chyba načtení obsahu“ a „Připravuji náhledy…“ jsou jen popsané výše (z artboardu odstraněny).

## 12. Texty a jazykové verze

- **Vykání v celé aplikaci** (rozhodnuto 6. 11.). Dnešní `cs.json` převážně tyká (cca 15 textů, např. „Zadej hledaný text.“, „Připoj zdrojový disk…“) → přepsat v rámci zadání. Návrh na plátně už vyká celý. Tlačítka v infinitivu („Zkusit znovu“, „Obnovit“, „Aktualizovat tuto složku“).
- **Pravidla pro nové texty:**
  1. každý text celá věta v jednom klíči, nikdy skládaný z kousků (pořadí slov se mezi jazyky liší),
  2. proměnné (cesta, název, počet) jen jako zástupné znaky uvnitř věty,
  3. tlačítka a štítky navrhovat tak, aby snesly delší překlad.
- Počty používají existující tvary `count.*.one/few/other` (1 fotka, 2 fotky, 5 fotek).
- Každý nový nebo změněný text má klíč v `cs.json` i `en.json` (viz oddíl 8).

## 13. Samostatné úkoly pro backend (mimo první verzi)

1. **Průběžný stav úlohy:** `RuntimeJob` doplnit o fázi, hotovo / celkem a čas poslední změny (vlákno úlohy je průběžně aktualizuje, čtou se pod zámkem v `snapshot()`); procenta jen u fází se známým celkem, u procházení disku jen počítadlo; odhad času počítá frontend.
2. **Bezpečné přerušení dlouhých úloh** (tlačítko Zastavit).
3. **Seskupené výsledky hledání:** dotaz vracející složky se shodou s počty a prvními N ukázkami na složku, stránkovaný po skupinách (oddíl 7a). Patří do redesignu, ne až po první verzi.
