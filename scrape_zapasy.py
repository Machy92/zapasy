import os
from bs4 import BeautifulSoup
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
import time
import sys
import re

# --- Nastavení ---
# Upravíme URL, aby explicitně obsahovaly matchFilter=1 pro odehrané zápasy
PHASES_TO_SCRAPE = [
    {"nazev": "Play-Off", "url_base": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2402&season=22&team=15076&showRound="},
    {"nazev": "Nadstavba - skupina A", "url_base": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2377&season=22&team=15076&showRound="},
    {"nazev": "Základní část", "url_base": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2317&season=22&team=15076&showRound="}
]
WARRIORS_TEAM_NAMES_ON_WEB = ["HSÚ SHC Warriors Chlumec", "Warriors Chlumec", "SHC Warriors Chlumec"] 
TEAM_NAME_FOR_DB = "Warriors Chlumec"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Chyba: SUPABASE_URL nebo SUPABASE_KEY nejsou nastaveny v prostředí!")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def parse_score(score_text):
    score_text = score_text.strip()
    if ':' not in score_text or "vs" in score_text.lower() or not any(char.isdigit() for char in score_text):
        return None, None
    parts = score_text.split(':')
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except (ValueError, IndexError):
        print(f"Chyba parsování skóre: '{score_text}'")
        return None, None

def determine_warriors_result(domaci_tym, hostujici_tym, domaci_skore, hostujici_skore):
    if domaci_skore is None or hostujici_skore is None: # Pokud zápas nemá skóre, nemá ani výsledek
        return None

    warriors_hráli_doma = any(name.lower() in domaci_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB)
    warriors_hráli_venku = any(name.lower() in hostujici_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB)

    if warriors_hráli_doma:
        if domaci_skore > hostujici_skore: return "vyhra"
        elif domaci_skore < hostujici_skore: return "prohra"
        else: return "remiza"
    elif warriors_hráli_venku:
        if hostujici_skore > domaci_skore: return "vyhra"
        elif hostujici_skore < domaci_skore: return "prohra"
        else: return "remiza"
    return None

def scrape_games_for_phase_playwright(url_with_filter, faze_nazev, is_future_game_scrape):
    print(f"Stahuji zápasy pro fázi: {faze_nazev} z URL: {url_with_filter} (pomocí Playwright)...")
    html_content = ""
    with sync_playwright() as p:
        browser = p.chromium.launch() 
        page = browser.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})
        try:
            print(f"Navštěvuji URL: {url_with_filter}")
            page.goto(url_with_filter, timeout=60000) 
            
            print("Čekám 3 sekundy na inicializaci stránky a JS...")
            time.sleep(3)

            cookie_button_selector = "button#c-p-bn"
            print(f"Zkouším najít a kliknout na cookie tlačítko: '{cookie_button_selector}'")
            try:
                page.click(cookie_button_selector, timeout=10000) 
                print("Cookie lišta úspěšně odkliknuta.")
                print("Čekám 5 sekund po odkliknutí cookie lišty...")
                time.sleep(5)
            except Exception as e:
                print(f"Cookie lišta nenalezena nebo se nepodařilo kliknout (pokračuji): {e}")
            
            first_game_card_selector = "div.d-md-flex.border-bottom" 
            print(f"Čekám na první kartu zápasu pomocí selektoru: '{first_game_card_selector}' (state='attached')...")
            page.wait_for_selector(first_game_card_selector, state="attached", timeout=30000)
            print("První karta zápasu nalezena v DOMu.")
            
            time.sleep(2) # Dáme chvilku na dokreslení
            html_content = page.content()

        except Exception as e:
            print(f"Chyba během Playwright operací: {e}")
            try:
                page.screenshot(path=f"error_screenshot_zapasy_{faze_nazev.replace(' ', '_')}.png")
                print(f"Screenshot uložen jako error_screenshot_zapasy_{faze_nazev.replace(' ', '_')}.png")
            except Exception as screenshot_error:
                print(f"Nepodařilo se uložit screenshot: {screenshot_error}")
            return [] 
        finally:
            browser.close()

    if not html_content:
        print("Nepodařilo se získat HTML obsah stránky pro zápasy.")
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    games_data = []
    
    game_cards = soup.select("div.d-md-flex.pt-3.pb-2.align-items-center.border-bcolor.border-bottom")
    
    if not game_cards:
        print(f"Nenalezeny žádné konkrétní karty zápasů v získaném HTML pro fázi '{faze_nazev}'.")
        return []
        
    print(f"Nalezeno {len(game_cards)} karet zápasů pro fázi '{faze_nazev}'. Parsuji...")

    for card in game_cards:
        try:
            date_time_container = card.select_one("div.typography.flex-shrink-0[style*='width: 115px']")
            datum_cas_text = "N/A"
            if date_time_container:
                date_p = date_time_container.select_one("p.font-size-normal")
                if date_p:
                    raw_date_text = date_p.decode_contents(formatter="html").replace("<br class=\"d-none d-md-block\"/>", " ").replace("<br/>", " ").replace("<br>", " ")
                    datum_cas_text = re.sub(r'\s+', ' ', raw_date_text).strip()
            
            teams_score_container = card.select_one("div.typography.flex-grow-1.d-flex")
            domaci_tym = "N/A"
            hostujici_tym = "N/A"
            domaci_skore_val, hostujici_skore_val = None, None
            odehrano = False

            if teams_score_container:
                teams_p = teams_score_container.select_one("p.font-weight-bold.font-size-normal")
                if teams_p:
                    team_names_raw = teams_p.find_all(string=True, recursive=False) 
                    team_names = [name.strip() for name in team_names_raw if name.strip()]
                    if not team_names:
                         team_names = [name.strip() for name in teams_p.get_text(separator="<br/>").split('<br/>') if name.strip()]
                    if len(team_names) >= 1: domaci_tym = team_names[0]
                    if len(team_names) >= 2: hostujici_tym = team_names[1]
                
                score_a = teams_score_container.select_one("div.beta a") 
                if score_a:
                    score_text = score_a.text.strip()
                    if "vs" in score_text.lower() or not score_text or not any(char.isdigit() for char in score_text) :
                        odehrano = False
                    else:
                        domaci_skore_val, hostujici_skore_val = parse_score(score_text)
                        odehrano = (domaci_skore_val is not None)
            
            # Pro budoucí zápasy bude výsledek vždy None a odehrano False, pokud skóre není vyplněno
            if is_future_game_scrape and not odehrano:
                vysledek_warriors = None
            else:
                vysledek_warriors = determine_warriors_result(domaci_tym, hostujici_tym, domaci_skore_val, hostujici_skore_val)

            # Normalizace názvů týmů pro Warriors
            current_domaci_tym = domaci_tym
            current_hostujici_tym = hostujici_tym
            if any(name.lower() in current_domaci_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB):
                domaci_tym = TEAM_NAME_FOR_DB
            if any(name.lower() in current_hostujici_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB):
                hostujici_tym = TEAM_NAME_FOR_DB

            game = {
                "datum_cas_text": datum_cas_text,
                "faze_souteze": faze_nazev,
                "domaci_tym": domaci_tym, # Normalizovaný název nebo původní
                "hostujici_tym": hostujici_tym, # Normalizovaný název nebo původní
                "domaci_skore": domaci_skore_val,
                "hostujici_skore": hostujici_skore_val,
                "odehrano": odehrano,
                "vysledek_warriors": vysledek_warriors
            }
            games_data.append(game)

        except Exception as e:
            print(f"Chyba při parsování karty zápasu: {e}. Karta HTML (začátek): {str(card)[:250]}...")
            continue
            
    print(f"Zpracováno {len(games_data)} zápasů pro fázi: {faze_nazev}.")
    return games_data

if __name__ == "__main__":
    all_games_to_db = []
    
    print("--- Stahuji ODEHRANÉ ZÁPASY (matchFilter=1) ---")
    for phase_info in PHASES_TO_SCRAPE:
        url_with_filter = phase_info["url_base"] + "&matchFilter=1"
        # Přidáme "(Odehrané)" k názvu fáze pro rozlišení v databázi, pokud chceme
        # nebo můžeme použít stejný název fáze a UPSERT se postará o aktualizaci
        games_this_phase = scrape_games_for_phase_playwright(url_with_filter, phase_info["nazev"], False)
        if games_this_phase:
            all_games_to_db.extend(games_this_phase)
        print(f"Malá pauza mezi fázemi (odehrané)...")
        time.sleep(3) # Krátká pauza

    print("\n--- Stahuji BUDOUCÍ ZÁPASY (matchFilter=2) ---")
    for phase_info in PHASES_TO_SCRAPE:
        url_with_filter = phase_info["url_base"] + "&matchFilter=2"
        # Přidáme "(Budoucí)" k názvu fáze, nebo necháme stejný
        games_this_phase_future = scrape_games_for_phase_playwright(url_with_filter, phase_info["nazev"], True)
        if games_this_phase_future:
            # Odstraníme duplicity, pokud by budoucí zápas už byl v seznamu (nemělo by se stát s UPSERT)
            for future_game in games_this_phase_future:
                is_duplicate = False
                for existing_game in all_games_to_db:
                    if (existing_game["datum_cas_text"] == future_game["datum_cas_text"] and
                        existing_game["domaci_tym"] == future_game["domaci_tym"] and
                        existing_game["hostujici_tym"] == future_game["hostujici_tym"]):
                        is_duplicate = True
                        break
                if not is_duplicate:
                    all_games_to_db.append(future_game)
        print(f"Malá pauza mezi fázemi (budoucí)...")
        time.sleep(3)
    
    if all_games_to_db:
        print(f"Celkem nalezeno {len(all_games_to_db)} unikátních záznamů o zápasech. Ukládám do Supabase...")
        try:
            response = supabase.table('zapasy').upsert(
                all_games_to_db, 
                on_conflict='datum_cas_text,domaci_tym,hostujici_tym' # Ujisti se, že toto unikátní omezení máš v DB
            ).execute()

            if hasattr(response, 'data') and response.data:
                 print(f"Úspěšně uloženo/aktualizováno {len(response.data)} záznamů o zápasech.")
            elif hasattr(response, 'error') and response.error:
                print(f"Chyba při ukládání do Supabase: {response.error}")
                print(f"Data, která se nepodařilo uložit (prvních 5): {all_games_to_db[:5]}")
            else:
                print("Nepodařilo se uložit žádná data, nebo odpověď neobsahuje očekávaná data.")
                print(f"Odpověď Supabase: {response}")
        except Exception as e:
            print(f"Výjimka při ukládání dat zápasů do Supabase: {e}")
    else:
        print("Nenalezeny žádné zápasy k uložení napříč všemi fázemi a filtry.")
        
    print("Skript pro stahování zápasů dokončen. 🔥")