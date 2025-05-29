import os
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
import re

# --- Nastavení ---
# Seznam fází soutěže s jejich URL a názvy
PHASES_TO_SCRAPE = [
    {"nazev": "Play-Off", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2402&season=22&team=15076&showRound=&matchFilter=1"},
    {"nazev": "Nadstavba - skupina A", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2377&season=22&team=15076&showRound=&matchFilter=1"},
    {"nazev": "Základní část", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2317&season=22&team=15076&showRound=&matchFilter=1"}
]
WARRIORS_TEAM_NAMES = ["HSÚ SHC Warriors Chlumec", "Warriors Chlumec", "Warriors Chlumec B", "SHC Warriors Chlumec"] # Přidej všechny varianty názvu

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Chyba: SUPABASE_URL nebo SUPABASE_KEY nejsou nastaveny v prostředí!")
    exit()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def parse_score(score_text):
    score_text = score_text.strip()
    if ':' not in score_text or "vs" in score_text.lower():
        return None, None
    parts = score_text.split(':')
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except (ValueError, IndexError):
        return None, None

def determine_warriors_result(domaci_tym, hostujici_tym, domaci_skore, hostujici_skore):
    if domaci_skore is None or hostujici_skore is None:
        return None

    warriors_hráli_doma = any(name.lower() in domaci_tym.lower() for name in WARRIORS_TEAM_NAMES)
    warriors_hráli_venku = any(name.lower() in hostujici_tym.lower() for name in WARRIORS_TEAM_NAMES)

    if warriors_hráli_doma:
        if domaci_skore > hostujici_skore: return "vyhra"
        elif domaci_skore < hostujici_skore: return "prohra"
        else: return "remiza"
    elif warriors_hráli_venku:
        if hostujici_skore > domaci_skore: return "vyhra"
        elif hostujici_skore < domaci_skore: return "prohra"
        else: return "remiza"
    return None

def scrape_games_for_phase(url, faze_nazev):
    print(f"Stahuji zápasy pro fázi: {faze_nazev} z URL: {url}")
    try:
        page = requests.get(url, timeout=10)
        page.raise_for_status() # Zkontroluje HTTP errory
    except requests.exceptions.RequestException as e:
        print(f"Chyba při stahování URL {url}: {e}")
        return []
        
    soup = BeautifulSoup(page.content, "html.parser")
    games_data = []
    
    # !!! KONTROLUJ A UPRAV TYTO SELEKTORY PODLE SKUTEČNÉ STRUKTURY WEBU !!!
    # Předpokládám, že každý zápas je v nějakém kontejneru.
    # Často to bývá <div class="match-item">, <article class="game-summary">, nebo <li> v <ul>
    # Zkus najít nejbližší společný opakující se element pro každý zápas.
    # Na stránce to vypadá, že zápasy jsou v <div class="col-12 col-md-6 mb-3">
    # a celý seznam je v <div class="games-list row">
    
    game_list_container = soup.select_one("div.games-list.row") # Hledáme hlavní kontejner se seznamem zápasů
    if not game_list_container:
        print(f"Hlavní kontejner zápasů ('div.games-list.row') nenalezen na URL: {url}")
        print("HTML stránky (prvních 1000 znaků):", page.text[:1000]) # Vypíše začátek HTML pro kontrolu
        return []

    # Hledáme jednotlivé karty zápasů uvnitř tohoto kontejneru
    game_cards = game_list_container.select("div[class*='col-md-6']") # Každý zápas je v takovém divu

    if not game_cards:
        print(f"Nenalezeny žádné karty zápasů ('div[class*=\'col-md-6\']') uvnitř 'div.games-list.row' na URL: {url}")
        return []

    print(f"Nalezeno {len(game_cards)} potenciálních karet zápasů pro fázi '{faze_nazev}'.")

    for card in game_cards:
        try:
            # DATUM A ČAS ZÁPASU
            # Hledej element, který obsahuje datum a čas. Může mít třídu jako "date-time", "game-date".
            # Příklad: <p class="text-muted small">SO 07.10.2023, 14:00 • Sportovní hala Chlumec</p>
            date_time_el = card.select_one("p.small") # Uprav tento selektor!
            datum_cas_text = "Neznámé datum"
            if date_time_el:
                full_text = date_time_el.get_text(separator=" ", strip=True)
                # Zkusíme extrahovat jen tu část s datem a časem
                match = re.search(r"([A-ZŽŠČŘĎŤŇÚŮÝÁÉÍÓ]{2}\s*\d{1,2}\.\d{1,2}\.\d{4},\s*\d{1,2}:\d{2})", full_text)
                if match:
                    datum_cas_text = match.group(1)
                else:
                    datum_cas_text = full_text.split("•")[0].strip() # Záložní řešení, vezme text před •

            # TÝMY
            # Domácí tým je často vlevo nebo nahoře, hostující vpravo nebo dole.
            # Hledej elementy s třídami jako "team-home-name", "team-away-name" nebo jen "team-name".
            # Na stránce to vypadá na: 
            # <div class="team-name team-name-home">...</div> a <div class="team-name team-name-away">...</div>
            home_team_el = card.select_one("div.team-name-home a, div.team-name-home span") # Uprav! Může být v <a> nebo <span>
            away_team_el = card.select_one("div.team-name-away a, div.team-name-away span") # Uprav!
            
            if not home_team_el or not away_team_el:
                # print(f"Přeskakuji kartu, nenalezeny oba týmy: {card.prettify()[:200]}") # Pro debug
                continue
            domaci_tym = home_team_el.text.strip()
            hostujici_tym = away_team_el.text.strip()

            # SKÓRE
            # Často v elementu s třídou "score", "result", nebo výrazným fontem.
            # Na stránce to je <div class="score"><span>DOM</span> : <span>HOST</span></div> nebo <div class="score">vs</div>
            score_el = card.select_one("div.score") # Uprav!
            domaci_skore_val, hostujici_skore_val = None, None
            if score_el:
                score_text = score_el.text.strip()
                if "vs" in score_text.lower():
                    odehrano = False
                else:
                    domaci_skore_val, hostujici_skore_val = parse_score(score_text)
                    odehrano = domaci_skore_val is not None
            else: # Pokud element score vůbec není
                odehrano = False


            vysledek_warriors = determine_warriors_result(domaci_tym, hostujici_tym, domaci_skore_val, hostujici_skore_val)

            game = {
                "datum_cas_text": datum_cas_text,
                "faze_souteze": faze_nazev,
                "domaci_tym": domaci_tym,
                "hostujici_tym": hostujici_tym,
                "domaci_skore": domaci_skore_val,
                "hostujici_skore": hostujici_skore_val,
                "odehrano": odehrano,
                "vysledek_warriors": vysledek_warriors
            }
            games_data.append(game)
            # print(f"Zpracován zápas: {datum_cas_text} | {domaci_tym} {domaci_skore_val} : {hostujici_skore_val} {hostujici_tym}")

        except Exception as e:
            print(f"Chyba při parsování karty zápasu: {e}. Karta: {card.text[:200]}...")
            continue
            
    print(f"Zpracováno {len(games_data)} zápasů pro fázi: {faze_nazev}.")
    return games_data

if __name__ == "__main__":
    all_games_to_db = []
    
    for phase_info in PHASES_TO_SCRAPE:
        games_this_phase = scrape_games_for_phase(phase_info["url"], phase_info["nazev"])
        if games_this_phase: # Přidáme jen pokud se něco našlo
            all_games_to_db.extend(games_this_phase)
    
    if all_games_to_db:
        print(f"Celkem nalezeno {len(all_games_to_db)} zápasů napříč fázemi. Ukládám do Supabase...")
        try:
            # Používáme 'ignore_duplicates=True' pokud by unikátní klíč selhal,
            # nebo lépe zajistit, že on_conflict je správně definován v tabulce a zde
            response = supabase.table('zapasy').upsert(
                all_games_to_db, 
                on_conflict='datum_cas_text,domaci_tym,hostujici_tym'
            ).execute()

            if hasattr(response, 'data') and response.data:
                 print(f"Úspěšně uloženo/aktualizováno {len(response.data)} záznamů o zápasech.")
            elif hasattr(response, 'error') and response.error:
                print(f"Chyba při ukládání do Supabase: {response.error}")
            else:
                print("Nepodařilo se uložit žádná data, nebo odpověď neobsahuje očekávaná data.")
        except Exception as e:
            print(f"Výjimka při ukládání dat zápasů do Supabase: {e}")
    else:
        print("Nenalezeny žádné zápasy k uložení napříč všemi fázemi.")
        
    print("Skript pro stahování zápasů dokončen. 🔥")