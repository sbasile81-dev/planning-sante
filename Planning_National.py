import streamlit as st
from supabase import create_client, Client
import pandas as pd
import calendar
import json
import os
import io
from datetime import datetime, date, timedelta
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT

# Identifiants Supabase (à remplacer par les vôtres)
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]


# Initialisation du client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MOIS_FR = {
    1: "JANVIER", 2: "FÉVRIER", 3: "MARS", 4: "AVRIL", 
    5: "MAI", 6: "JUIN", 7: "JUILLET", 8: "AOÛT", 
    9: "SEPTEMBRE", 10: "OCTOBRE", 11: "NOVEMBRE", 12: "DÉCEMBRE"
}

# --- 1. CONFIGURATION ---
st.set_page_config(page_title="Validateur National Santé", layout="wide")
#FILE_PATH = "config_national_final.json"

# --- 2. FONCTIONS DE GESTION DES DONNÉES ---

def charger_donnees():
    try:
        # 1. Récupération de la configuration (ligne avec ID 1)
        res_conf = supabase.table("configuration").select("*").eq("id", 1).execute()
        
        # 2. Récupération de la liste complète des agents
        res_agents = supabase.table("base_agents").select("*").execute()
        
        data = {
            'config': {}, 
            'composition': {}, 
            'conges': [], 
            'liste_unites': ["Unité de Soins"],
            'base_agents': []
        }

        # Traitement de la configuration
        if res_conf.data:
            conf = res_conf.data[0]
            data['config'] = conf.get('config_globale', {})
            data['composition'] = conf.get('composition_equipes', {})
            data['liste_unites'] = conf.get('liste_unites', ["Unité de Soins"])
            
            # Conversion des dates de congés (format texte -> format date Python)
            conges_bruts = conf.get('conges', [])
            for c in conges_bruts:
                c['debut'] = datetime.strptime(c['debut'], "%Y-%m-%d").date()
                c['fin'] = datetime.strptime(c['fin'], "%Y-%m-%d").date()
            data['conges'] = conges_bruts
            
        # Traitement des agents
        if res_agents.data:
            data['base_agents'] = res_agents.data
            
        return data
    except Exception as e:
        st.error(f"Erreur de chargement Cloud : {e}")
        return None

def sauvegarder_donnees():
    try:
        # 1. Sauvegarde des agents (Gestion du conflit sur le NOM pour éviter les doublons)
        for agent in st.session_state.get('base_agents', []):
            agent_payload = {
                "nom": agent["nom"],
                "emploi": agent.get("emploi", ""),
                "matricule": agent.get("matricule", "")
            }
            supabase.table("base_agents").upsert(agent_payload, on_conflict="nom").execute()

        # 2. Sauvegarde de la configuration globale
        config_payload = {
            "id": 1,
            "liste_unites": st.session_state.get('liste_unites', ["Unité de Soins"]),
            "config_globale": st.session_state.get('config', {}),
            "composition_equipes": st.session_state.get('composition', {}),
            "conges": [{**c, "debut": str(c['debut']), "fin": str(c['fin'])} 
                       for c in st.session_state.get('conges', [])]
        }
        supabase.table("configuration").upsert(config_payload).execute()
        
    except Exception as e:
        st.error(f"Erreur de sauvegarde Cloud : {e}")

# =========================================================
# 3...ALGORITHME DE PLANNING NATIONAL SANTÉ - VERSION FINALE (CORRIGÉE V13)
# =========================================================
def calculer_planning(annee, mois, nb_equipes):
    import calendar
    from datetime import date
    jours_dans_mois = calendar.monthrange(annee, mois)[1]
    u_active = st.session_state.config.get('unite_active', 'Unité de Soins')
    
    heures_hebdo = {}
    planning_temp = {}
    toutes_les_gardes = {}
    index_rotation = st.session_state.get('last_index', 0) 

    # --- PASSE 1 : ATTRIBUTION DES GARDES ---
    for j in range(1, jours_dans_mois + 1):
        dt = date(annee, mois, j)
        decalage = 0
        trouve = False
        while not trouve and decalage < nb_equipes:
            test_id = ((index_rotation + decalage) % nb_equipes) + 1
            membres_test = st.session_state.composition.get(f"{u_active}_{test_id}", [])
            # Uniquement ceux qui ne sont pas en congé
            presents = [n for n in membres_test if not any(c['agent'] == n and c['debut'] <= dt <= c['fin'] for c in st.session_state.conges)]
            
            if presents:
                toutes_les_gardes[j] = test_id
                index_rotation = (test_id % nb_equipes)
                trouve = True
            else:
                decalage += 1
        if not trouve: toutes_les_gardes[j] = None

    # --- PASSE 2 : SERVICE ET LISSAGE "ZÉRO REPOS ORDINAIRE" ---
    for j in range(1, jours_dans_mois + 1):
        dt = date(annee, mois, j)
        sem_key = f"Sem {dt.isocalendar()[1]}"
        is_we = dt.weekday() >= 5
        id_equipe_de_garde = toutes_les_gardes.get(j)

        for e_id in range(1, nb_equipes + 1):
            membres = st.session_state.composition.get(f"{u_active}_{e_id}", [])
            for n in membres:
                if n not in heures_hebdo: heures_hebdo[n] = {}
                if sem_key not in heures_hebdo[n]:
                    # Récupération du report (mémoire) uniquement en début de mois
                    report = st.session_state.get('memoire_heures', {}).get(n, 0)
                    heures_hebdo[n][sem_key] = report if j <= 7 else 0

                cur_h = heures_hebdo[n][sem_key]
                h_dispo = 40 - cur_h

                # --- CAS 1 : PRIORITÉS ABSOLUES ---
                # Congé
                if any(c['agent'] == n and c['debut'] <= dt <= c['fin'] for c in st.session_state.conges):
                    planning_temp[(n, j)] = {"type": "Congé", "heures": 0}
                    continue
                # Garde (0h dans le quota 40h)
                if e_id == id_equipe_de_garde:
                    planning_temp[(n, j)] = {"type": "Garde", "heures": 0}
                    continue
                # Repos J+1 (Obligatoire après garde)
                if planning_temp.get((n, j-1), {}).get("type") == "Garde":
                    planning_temp[(n, j)] = {"type": "Repos", "heures": 0}
                    continue

                # --- CAS 2 : JOURS DE SEMAINE (SERVICE OBLIGATOIRE) ---
                if not is_we:
                    if h_dispo >= 10:
                        planning_temp[(n, j)] = {"type": "Journée", "heures": 10}
                        heures_hebdo[n][sem_key] += 10
                    elif h_dispo >= 5:
                        planning_temp[(n, j)] = {"type": "Demi-journée", "heures": 5}
                        heures_hebdo[n][sem_key] += 5
                    else:
                        # Si déjà 40h, on ne peut plus ajouter d'heures. 
                        # On marque "Repos" mais c'est un repos forcé par le quota légal.
                        planning_temp[(n, j)] = {"type": "Repos", "heures": 0}

                # --- CAS 3 : WEEK-END (FLEXIBILITÉ) ---
                else:
                    # On travaille le WE seulement pour boucher le trou jusqu'à 35h-40h
                    if cur_h < 35:
                        h_we = 10 if h_dispo >= 10 else 5
                        planning_temp[(n, j)] = {"type": "Week-end", "heures": h_we}
                        heures_hebdo[n][sem_key] += h_we
                    else:
                        # Repos de week-end classique (Non listé sur le programme)
                        planning_temp[(n, j)] = {"type": "", "heures": 0}

    return planning_temp, heures_hebdo
                            
    # Enregistrement pour le mois suivant
    derniere_sem = f"Semaine {date(annee, mois, jours_dans_mois).isocalendar()[1]}"
    cle_actuelle = f"{annee}_{mois}"
    if "memoire_heures" not in st.session_state: st.session_state.memoire_heures = {}
    st.session_state.memoire_heures[cle_actuelle] = {n: heures_hebdo[n].get(derniere_sem, 0) for n in heures_hebdo}
    
    return planning_temp, heures_hebdo

# --- 4. EXPORT WORD ---
def exporter_vers_word(recap_data, config, mois_num, annee_num):
    from io import BytesIO
    doc = Document()
    section = doc.sections[-1]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width

    # En-tête
    header_table = doc.add_table(rows=1, cols=2)
    c1, c2 = header_table.rows[0].cells
    c1.text = f"MINISTERE DE LA SANTE\n-----------------\nREGION DU {config.get('region','')}\n-----------------\nDISTRICT DE {config.get('district','')}\n-----------------\n{config.get('nom_csps','')}"
    c2.text = "BURKINA FASO\n-----------------\nLa Patrie ou la mort, Nous vaincrons"
    c2.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    # Titre
    unite = config.get('unite_active', 'SERVICE').upper()
    titre = doc.add_paragraph(f"\nPROGRAMME DE TRAVAIL DU MOIS DE {MOIS_FR[mois_num]} {annee_num} ({unite})")
    titre.alignment = WD_ALIGN_PARAGRAPH.CENTER
    titre.runs[0].bold = True

    # Tableau
    table = doc.add_table(rows=2, cols=9); table.style = 'Table Grid'
    h1 = table.rows[0].cells; h1[0].text = "Eq"; h1[1].text = "Nom & Prénoms"; h1[2].text = "Emploi"; h1[3].text = "Matricule"
    h1[4].merge(h1[5]).merge(h1[6]); h1[4].text = "Travail de jour"; h1[7].text = "Garde"; h1[8].text = "Repos"
    h2 = table.rows[1].cells; h2[4].text = "7h30-17h30"; h2[5].text = "7h30-12h30"; h2[6].text = "Week-end"
    for i in [0,1,2,3,7,8]: table.cell(0,i).merge(table.cell(1,i))

    for entry in recap_data:
        membres = entry['Membres'].split(" / ")
        first_row_idx = len(table.rows)
        for idx, nom_complet in enumerate(membres):
            row = table.add_row().cells
            nom_nettoye = nom_complet.replace(" (🏖️)", "").strip()
            agent_info = next((a for a in st.session_state.base_agents if a['nom'].strip() == nom_nettoye), {})
            row[1].text = nom_nettoye; row[2].text = agent_info.get('emploi', ''); row[3].text = agent_info.get('matricule', '')
            if idx == 0:
                row[0].text = str(entry['N°']); row[4].text = entry.get('Journée', ''); row[5].text = entry.get('Demi-journée', '')
                row[6].text = entry.get('Week-end et fériés', ''); row[7].text = entry.get('Garde', ''); row[8].text = entry.get('Repos', '')
        if len(membres) > 1:
            for col_idx in [0, 4, 5, 6, 7, 8]: table.cell(first_row_idx, col_idx).merge(table.cell(len(table.rows)-1, col_idx))

    # Signatures
    doc.add_paragraph("\n")
    sig_table = doc.add_table(rows=1, cols=2)
    s1, s2 = sig_table.rows[0].cells
    s1.text = f"L’Infirmier chef de poste\n\n\n{config.get('nom_icp', 'ICP')}"
    s2.text = f"Le Médecin Chef du District\n\n\n{config.get('nom_mcd', 'MCD')}"
    s2.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    target = io.BytesIO(); doc.save(target); target.seek(0)
    return target

# --- 5. INITIALISATION ---
if 'config' not in st.session_state:
    saved = charger_donnees()
    if saved:
        # Récupération des données existantes
        st.session_state.config = saved.get('config', {"Etablissement": "CSPS", "unite_active": "Unité de Soins"})
        st.session_state.composition = saved.get('composition', {})
        st.session_state.conges = saved.get('conges', [])
        st.session_state.liste_unites = saved.get('liste_unites', ["Unité de Soins"])
        st.session_state.base_agents = saved.get('base_agents', [])
        
        # --- ÉLÉMENT CLÉ POUR LE LISSAGE ---
        # On récupère la mémoire des heures du mois précédent
        st.session_state.memoire_heures = saved.get('memoire_heures', {})
    else:
        # Valeurs par défaut si le fichier n'existe pas encore
        st.session_state.config = {"Etablissement": "CSPS", "unite_active": "Unité de Soins"}
        st.session_state.composition = {}
        st.session_state.conges = []
        st.session_state.liste_unites = ["Unité de Soins"]
        st.session_state.base_agents = []
        st.session_state.memoire_heures = {}

# --- 6. BARRE LATÉRALE ---
with st.sidebar:
    st.header("⚙️ Paramètres")
    with st.expander("🏥 Gérer les Unités"):
        nouvelle = st.text_input("Ajouter unité")
        if st.button("➕ Ajouter") and nouvelle:
            st.session_state.liste_unites.append(nouvelle); sauvegarder_donnees(); st.rerun()
    
    st.session_state.config["region"] = st.text_input("Région", value=st.session_state.config.get("region", ""))
    st.session_state.config["district"] = st.text_input("District", value=st.session_state.config.get("district", ""))
    st.session_state.config["nom_csps"] = st.text_input("CSPS", value=st.session_state.config.get("nom_csps", ""))
    st.session_state.config["nom_icp"] = st.text_input("Nom ICP", value=st.session_state.config.get("nom_icp", ""))
    st.session_state.config["nom_mcd"] = st.text_input("Nom MCD", value=st.session_state.config.get("nom_mcd", ""))
    
    u_active = st.selectbox("Unité Active", st.session_state.liste_unites, index=st.session_state.liste_unites.index(st.session_state.config.get("unite_active", st.session_state.liste_unites[0])))
    st.session_state.config["unite_active"] = u_active
    nb_eq = st.slider("Équipes", 2, 15, value=st.session_state.config.get("nb_equipes", 6))
    st.session_state.config["nb_equipes"] = nb_eq
    annee = st.number_input("Année", value=2026); mois = st.slider("Mois", 1, 12, value=3)
    if st.button("💾 Sauvegarder Config"): sauvegarder_donnees(); st.success("Enregistré !")

# --- 7. CALCUL ---
planning_final, heures_hebdo = calculer_planning(annee, mois, nb_eq)

# --- 8. ONGLETS ---
t1, t2, t3, t4, t5 = st.tabs(["✅ Validation", "📊 Vue Regroupée", "👥 Équipes", "🏖️ Congés", "🗂️ Base Agents"])

with t1:
    for nom, semaines in heures_hebdo.items():
        with st.expander(f"👤 {nom}"):
            cols = st.columns(len(semaines))
            for i, (s, total) in enumerate(semaines.items()):
                if 35 <= total <= 40: cols[i].success(f"{s}: {total}h")
                elif total > 40: cols[i].error(f"{s}: {total}h")
                else: cols[i].warning(f"{s}: {total}h")

with t2:
    recap = []
    for i in range(1, nb_eq + 1):
        membres_bruts = st.session_state.composition.get(f"{u_active}_{i}", [])
        jrs = {"Journée": [], "Demi-journée": [], "Week-end": [], "Garde": [], "Repos": []}
        if membres_bruts:
            for d in range(1, calendar.monthrange(annee, mois)[1] + 1):
                shift = {}
                for agent in membres_bruts:
                    s = planning_final.get((agent, d), {})
                    if s.get("type") != "Congé": shift = s; break
                t = shift.get("type")
                if t in jrs: jrs[t].append(str(d))
                elif t == "Garde + Demi-journée": jrs["Demi-journée"].append(str(d)); jrs["Garde"].append(str(d))
        noms = [f"{m} (🏖️)" if any(c['agent']==m for c in st.session_state.conges) else m for m in membres_bruts]
        recap.append({"N°": i, "Membres": " / ".join(noms), "Journée": ", ".join(jrs["Journée"]), "Demi-journée": ", ".join(jrs["Demi-journée"]), "Week-end et fériés": ", ".join(jrs["Week-end"]), "Garde": ", ".join(jrs["Garde"]), "Repos": ", ".join(jrs["Repos"])})
    st.table(pd.DataFrame(recap))
    if recap:
        st.download_button("📄 Exporter WORD", data=exporter_vers_word(recap, st.session_state.config, mois, annee), file_name=f"Planning_{u_active}.docx")

with t3:
    st.subheader("👥 Équipes (Sans doublons)")
    options_agents = [a["nom"] for a in st.session_state.base_agents]
    tous_affectes = []
    for eid in range(1, nb_eq + 1): tous_affectes.extend(st.session_state.composition.get(f"{u_active}_{eid}", []))
    
    cols = st.columns(3)
    for i in range(1, nb_eq + 1):
        key = f"{u_active}_{i}"
        anciens = st.session_state.composition.get(key, [])
        dispo = [a for a in options_agents if a not in [x for x in tous_affectes if x not in anciens]]
        res = cols[(i-1)%3].multiselect(f"Équipe {i}", options=dispo, default=anciens, key=f"sel_{key}")
        if res != anciens: st.session_state.composition[key] = res; sauvegarder_donnees(); st.rerun()

with t4:
    with st.form("f_conge"):
        c_nom = st.selectbox("Agent", [a["nom"] for a in st.session_state.base_agents])
        d1, d2 = st.columns(2); b = d1.date_input("Début"); f = d2.date_input("Fin")
        if st.form_submit_button("Enregistrer"):
            st.session_state.conges.append({"agent": c_nom, "debut": b, "fin": f}); sauvegarder_donnees(); st.rerun()
    if st.session_state.conges: st.table(pd.DataFrame(st.session_state.conges))

with t5:
    st.subheader("🗂️ Gestion des Agents")
    if st.session_state.base_agents:
        df_agents = pd.DataFrame(st.session_state.base_agents)
        # Permet la modification directe et la suppression (icône poubelle)
        edited_df = st.data_editor(df_agents, num_rows="dynamic", key="agent_editor", use_container_width=True)
        if st.button("💾 Appliquer les changements (Modifs & Suppressions)"):
            st.session_state.base_agents = edited_df.to_dict('records')
            sauvegarder_donnees()
            st.success("Base mise à jour !")
            st.rerun()
    
    with st.expander("➕ Ajouter un nouvel agent"):
        with st.form("new_agent_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            n = c1.text_input("Nom & Prénoms")
            e = c2.text_input("Emploi")
            m = c3.text_input("Matricule")
            if st.form_submit_button("Enregistrer"):
                if n:
                    st.session_state.base_agents.append({"nom":n,"emploi":e,"matricule":m})
                    sauvegarder_donnees(); st.rerun()




