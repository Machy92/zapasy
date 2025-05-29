import os
import requests # Stále můžeme potřebovat pro jiné věci, ale Playwright bude hlavní
from bs4 import BeautifulSoup
from supabase import create_client, Client
from playwright.sync_api import sync_playwright
import time
import sys
import re

# --- Nastavení ---
PHASES_TO_SCRAPE = [
    {"nazev": "Play-Off", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2402&season=22&team=15076&showRound=&matchFilter=1"},
    {"nazev": "Nadstavba - skupina A", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2377&season=22&team=15076&showRound=&matchFilter=1"},
    {"nazev": "Základní část", "url": "https://cechysever.cmshb.cz/tym?id=358&page=games&competition=866&part=2317&season=22&team=15076&showRound=&matchFilter=1"}
]
# Názvy, pod kterými se Warriors mohou objevit na webu
WARRIORS_TEAM_NAMES_ON_WEB = ["HSÚ SHC Warriors Chlumec", "Warriors Chlumec", "SHC Warriors Chlumec"] 
# Název, pod kterým chceme Warriors ukládat do DB
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
    if domaci_skore is None or hostujici_skore is None:
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

def scrape_games_for_phase_playwright(url, faze_nazev):
    print(f"Stahuji zápasy pro fázi: {faze_nazev} z URL: {url} (pomocí Playwright)...")
    html_content = ""
    with sync_playwright() as p:
        browser = p.chromium.launch() 
        page = browser.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})
        try:
            print(f"Navštěvuji URL: {url}")
            page.goto(url, timeout=60000) 
            
            print("Čekám 3 sekundy na inicializaci stránky a JS...")
            time.sleep(3)

            # Pokus o odkliknutí cookie lišty (ID "c-p-bn" je pro "Přijmout vše")
            cookie_button_selector = "button#c-p-bn"
            print(f"Zkouším najít a kliknout na cookie tlačítko: '{cookie_button_selector}'")
            try:
                page.click(cookie_button_selector, timeout=10000) 
                print("Cookie lišta úspěšně odkliknuta.")
                print("Čekám 3 sekundy po odkliknutí cookie lišty...")
                time.sleep(3)
            except Exception as e:
                print(f"Cookie lišta nenalezena nebo se nepodařilo kliknout (pokračuji): {e}")
            
            # Čekáme, až se objeví první prvek zápasu na stránce
            # Na základě tvého HTML to vypadá, že každý zápas je v divu s touto strukturou tříd:
            # <div class="d-md-flex pt-3 pb-2 align-items-center border-bcolor border-bottom">
            # Zkusíme počkat na první takový prvek.
            first_game_card_selector = "div.d-md-flex.border-bottom" # Obecnější selektor
            print(f"Čekám na první kartu zápasu pomocí selektoru: '{first_game_card_selector}'...")
            page.wait_for_selector(first_game_card_selector, state="attached", timeout=30000)
            print("První karta zápasu nalezena v DOMu.")
            
            # Dáme ještě chvilku na dokreslení
            time.sleep(2)
            html_content = page.content()

        except Exception as e:
            print(f"Chyba během Playwright operací: {e}")
            try:
                page.screenshot(path="error_screenshot_zapasy.png")
                print("Screenshot uložen jako error_screenshot_zapasy.png")
            except Exception as screenshot_error:
                print(f"Nepodařilo se uložit screenshot: {screenshot_error}")
            return [] # Při chybě Playwright vracíme prázdný seznam
        finally:
            browser.close()

    if not html_content:
        print("Nepodařilo se získat HTML obsah stránky pro zápasy.")
        return []

    soup = BeautifulSoup(html_content, "html.parser")
    games_data = []
    
    # Selektor pro jednotlivé karty zápasů - na základě tvého HTML kódu
    game_cards = soup.select("div.d-md-flex.pt-3.pb-2.align-items-center.border-bcolor.border-bottom")
    
    if not game_cards:
        print(f"Nenalezeny žádné konkrétní karty zápasů pomocí selektoru 'div.d-md-flex.border-bottom' v získaném HTML.")
        # print(f"HTML (prvních 2000 znaků): {html_content[:2000]}") # Pro debug
        return []
        
    print(f"Nalezeno {len(game_cards)} karet zápasů pro fázi '{faze_nazev}'. Parsuji...")

    for card in game_cards:
        try:
            # DATUM A ČAS ZÁPASU
            # Hledáme <div class="typography ... flex-shrink-0" style="width: 115px"> <p class="mb-0 font-size-normal">...</p> </div>
            date_time_container = card.select_one("div.typography.flex-shrink-0[style*='width: 115px']")
            datum_cas_text = "N/A"
            if date_time_container:
                date_p = date_time_container.select_one("p.font-size-normal")
                if date_p:
                    # Bereme celý text, může obsahovat <br>
                    raw_date_text = date_p.decode_contents(formatter="html").replace("<br class=\"d-none d-md-block\"/>", " ").replace("<br/>", " ").replace("<br>", " ")
                    datum_cas_text = re.sub(r'\s+', ' ', raw_date_text).strip()
            
            # TÝMY A SKÓRE
            # Hledáme <div class="typography ... flex-grow-1 ..."> <p>Domácí<br>Hosté</p> <div>Skóre</div> </div>
            teams_score_container = card.select_one("div.typography.flex-grow-1.d-flex")
            domaci_tym = "N/A"
            hostujici_tym = "N/A"
            domaci_skore_val, hostujici_skore_val = None, None
            odehrano = False

            if teams_score_container:
                teams_p = teams_score_container.select_one("p.font-weight-bold.font-size-normal")
                if teams_p:
                    # inner_html = teams_p.decode_contents(formatter="html") # Získá HTML obsah včetně <br>
                    # team_names = [name.strip() for name in inner_html.split('<br/>') if name.strip()]
                    team_names_raw = teams_p.find_all(string=True, recursive=False) # Zkusí vzít jen přímé texty
                    team_names = [name.strip() for name in team_names_raw if name.strip()]
                    if not team_names : # záložní pokud jsou texty vnořené hlouběji nebo je to jeden text s <br>
                         team_names = [name.strip() for name in teams_p.get_text(separator="<br/>").split('<br/>') if name.strip()]


                    if len(team_names) >= 1:
                        domaci_tym = team_names[0]
                    if len(team_names) >= 2:
                        hostujici_tym = team_names[1]
                
                score_a = teams_score_container.select_one("div.beta a") # Skóre je v odkazu
                if score_a:
                    score_text = score_a.text.strip()
                    if "vs" in score_text.lower() or not score_text:
                        odehrano = False
                    else:
                        domaci_skore_val, hostujici_skore_val = parse_score(score_text)
                        odehrano = (domaci_skore_val is not None)
            
            vysledek_warriors = determine_warriors_result(domaci_tym, hostujici_tym, domaci_skore_val, hostujici_skore_val)

            # Normalizace názvů týmů pro Warriors
            if any(name.lower() in domaci_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB):
                domaci_tym = TEAM_NAME_FOR_DB
            if any(name.lower() in hostujici_tym.lower() for name in WARRIORS_TEAM_NAMES_ON_WEB):
                hostujici_tym = TEAM_NAME_FOR_DB

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

        except Exception as e:
            print(f"Chyba při parsování karty zápasu: {e}. Karta HTML (začátek): {str(card)[:250]}...")
            continue
            
    print(f"Zpracováno {len(games_data)} zápasů pro fázi: {faze_nazev}.")
    return games_data

if __name__ == "__main__":
    all_games_to_db = []
    
    for phase_info in PHASES_TO_SCRAPE:
        games_this_phase = scrape_games_for_phase_playwright(phase_info["url"], phase_info["nazev"])
        if games_this_phase:
            all_games_to_db.extend(games_this_phase)
        print(f"Malá pauza mezi fázemi...")
        time.sleep(5) # Přidáme pauzu mezi stahováním jednotlivých fází
    
    if all_games_to_db:
        print(f"Celkem nalezeno {len(all_games_to_db)} zápasů napříč fázemi. Ukládám do Supabase...")
        try:
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
                print(f"Odpověď Supabase: {response}")
        except Exception as e:
            print(f"Výjimka při ukládání dat zápasů do Supabase: {e}")
    else:
        print("Nenalezeny žádné zápasy k uložení napříč všemi fázemi.")
        
    print("Skript pro stahování zápasů dokončen. 🔥")