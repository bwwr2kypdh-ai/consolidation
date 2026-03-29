import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import pandas as pd
import numpy as np
from shapely.geometry import Polygon
import math
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. PAGE CONFIGURATION & CSS
# ==========================================
st.set_page_config(layout="wide", page_title="Port Surcharge Optimizer PRO")

st.markdown("""
<style>
@media print {
    .stSidebar {display: none !important;}
    header {display: none !important;}
    footer {display: none !important;}
    .stButton {display: none !important;}
}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. AUTHENTICATION
# ==========================================
def check_password():
    def password_entered():
        if st.session_state["pwd_input"].strip().lower() == "admin":
            st.session_state["authenticated"] = True
            del st.session_state["pwd_input"] 
        else:
            st.session_state["authenticated"] = False

    if not st.session_state.get("authenticated", False):
        st.title("🔒 Accès Sécurisé - Simulateur Portuaire")
        st.text_input("Code d'accès (tapez 'admin' et Entrée) :", type="password", on_change=password_entered, key="pwd_input")
        if "authenticated" in st.session_state and not st.session_state["authenticated"]:
            st.error("Mot de passe incorrect.")
        st.stop()

check_password()
if st.sidebar.button("Se déconnecter 🚪"):
    st.session_state["authenticated"] = False
    st.rerun()

# ==========================================
# 3. CORE ENGINEERING MATH
# ==========================================
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    base_fill_height = target_load / gamma_fill
    return final_load, target_load, base_fill_height

def calc_settlement_oedometer_layered(layers, delta_sigma):
    total_S = 0.0
    for L in layers:
        H, e0, Cc, Cr, sig_0, sig_c = L['H'], L['e0'], L['Cc'], L['Cr'], L['sig_0'], L['sig_c']
        final_stress = sig_0 + delta_sigma
        if final_stress <= sig_c:
            total_S += H * (Cr / (1 + e0)) * math.log10(final_stress / sig_0)
        else:
            S_recomp = H * (Cr / (1 + e0)) * math.log10(sig_c / sig_0)
            S_virgin = H * (Cc / (1 + e0)) * math.log10(final_stress / sig_c)
            total_S += (S_recomp + S_virgin)
    return total_S

# --- NOUVEAU : CALCUL DU FLUAGE (SECONDARY COMPRESSION) ---
def calc_secondary_compression(layers, t_primary_days, t_design_life_years):
    total_S_sec = 0.0
    # Convertir le temps en années pour la formule
    t1 = t_primary_days / 365.25
    t2 = t1 + t_design_life_years
    
    if t1 <= 0 or t2 <= 0: return 0.0
    
    for L in layers:
        # Formule : S_sec = C_alpha * H * log10(t2 / t1)
        C_alpha = L['C_alpha']
        H = L['H']
        total_S_sec += C_alpha * H * math.log10(t2 / t1)
    return total_S_sec

# --- NOUVEAU : CALCUL RISQUE DE RUPTURE (MUDWAVE) ---
def check_mudwave_risk(Su_min, gamma_fill, H_fill):
    # Capacité portante ultime d'une argile molle (Nc = 5.14)
    q_ult = 5.14 * Su_min
    q_applied = gamma_fill * H_fill
    FS = q_ult / q_applied if q_applied > 0 else 999
    
    # Hauteur maximale de levée sécuritaire (FS = 1.3 minimum)
    H_max_safe = q_ult / (gamma_fill * 1.3)
    
    return FS, q_ult, q_applied, H_max_safe

# (Autres fonctions existantes simplifiées pour la lisibilité)
def hansbo_consolidation(ch_m2_yr, spacing, t_days):
    ch_day = ch_m2_yr / 365.25
    D = 1.05 * spacing 
    dw = 0.052 
    n = D / dw
    Fn = math.log(n) - 0.75
    Tr = (ch_day * t_days) / (D**2)
    try: Ur = 1.0 - math.exp((-8.0 * Tr) / Fn)
    except: Ur = 1.0
    return max(0.0, min(Ur, 1.0))

if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# ==========================================
# 4. SIDEBAR PARAMETERS (AVEC PARAMÈTRES AVANCÉS)
# ==========================================
st.sidebar.header("🏗️ Charges & Durée de Vie")
dead_load = st.sidebar.number_input("Dead Load (Chaussée) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Exploitation) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input("Poids Volumique Remblai [kN/m³]", value=19.0, step=0.5)
design_life = st.sidebar.number_input("Durée de vie de l'ouvrage [Années]", value=30, step=10)

st.sidebar.markdown("---")
st.sidebar.header("🗜️ Stratigraphie & Paramètres Avancés")
num_layers = st.sidebar.number_input("Nombre de couches", min_value=1, max_value=5, value=1)

soil_layers = []
for i in range(int(num_layers)):
    with st.sidebar.expander(f"Couche {i+1}", expanded=(i==0)):
        H = st.number_input(f"Épaisseur (m)", value=8.0, key=f"H_{i}")
        e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
        Cc = st.number_input("Cc (Compression)", value=0.45, key=f"Cc_{i}")
        Cr = st.number_input("Cr (Recompression)", value=0.05, key=f"Cr_{i}")
        sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
        sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
        
        st.caption("Paramètres de Risque (Nouveau)")
        C_alpha = st.number_input("C_alpha (Fluage)", value=0.015, format="%.3f", help="Indice de compression secondaire", key=f"ca_{i}")
        Su = st.number_input("Su (Cohésion non drainée) [kPa]", value=15.0, help="Résistance au cisaillement pour le calcul de rupture", key=f"su_{i}")
        
        soil_layers.append({
            'H': H, 'e0': e0, 'Cc': Cc, 'Cr': Cr, 'sig_0': sig_0, 'sig_c': sig_c, 
            'C_alpha': C_alpha, 'Su': Su
        })

total_H = sum(layer['H'] for layer in soil_layers)

st.sidebar.markdown("---")
st.sidebar.header("🚰 Drainage Vertical (PVD)")
ch_val = st.sidebar.number_input("c_h [m²/yr]", value=2.0, step=0.5)
pvd_spacing = st.sidebar.slider("Espacement PVD (m)", 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input("Temps Cible (Jours)", value=180, step=10)

# ==========================================
# 5. MAIN UI & CALCULATIONS
# ==========================================
st.title("⚓ Port Terminal Optimizer PRO (Fluage & Rupture)")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles='CartoDB Positron')
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])
with col1: output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader("Exécution du Modèle")
    if st.button("🚀 CALCULER LE PROJET", use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error("🛑 Veuillez dessiner un polygone.")
        else:
            poly = Polygon(poly_coords)
            c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
            area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
            
            # 1. Calcul des charges
            final_load, target_load, base_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
            
            # 2. Tassements Primaire (Avant vs Après Traitement)
            S_primary_untreated = calc_settlement_oedometer_layered(soil_layers, final_load)
            S_primary_treated = calc_settlement_oedometer_layered(soil_layers, target_load)
            
            # 3. Tassement Secondaire (Fluage)
            S_secondary = calc_secondary_compression(soil_layers, target_time, design_life)
            
            # 4. Hauteur finale requise
            actual_fill_height = base_fill_height + S_primary_treated
            volume_m3 = area_m2 * actual_fill_height
            
            # 5. Risque de Rupture (Mudwave) basé sur la couche la plus faible
            min_Su = min(layer['Su'] for layer in soil_layers)
            FS_mudwave, q_ult, q_applied, H_max_safe = check_mudwave_risk(min_Su, gamma_fill, actual_fill_height)

            st.session_state['project_data'] = {
                'area_m2': area_m2, 'final_load': final_load, 'target_load': target_load,
                'S_untreated': S_primary_untreated, 'S_treated': S_primary_treated,
                'S_secondary': S_secondary, 'actual_height': actual_fill_height,
                'volume_m3': volume_m3, 'FS_mudwave': FS_mudwave, 'H_max_safe': H_max_safe,
                'min_Su': min_Su
            }
            st.success("✅ Modélisation complète réussie.")

# ==========================================
# 6. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_geo, tab_risk, tab_log = st.tabs([
        "📊 Tassement Avant/Après", 
        "⚠️ Analyse des Risques (Fluage & Mudwave)", 
        "🚜 Quantitatifs"
    ])
    
    # --- ONGLET 1 : AVANT / APRÈS ---
    with tab_geo:
        st.subheader("Comparatif : Nécessité du Traitement de Sol")
        st.write("Le graphique ci-dessous démontre le tassement que subirait l'ouvrage pendant sa phase d'exploitation si aucun remblai de préchargement n'était appliqué.")
        
        # Data preparation for the chart
        categories = ['SANS Traitement (Charge de service seule)', 'AVEC Traitement (Préchargement)']
        primary_settlement = [pd_data['S_untreated'], pd_data['S_treated']]
        secondary_settlement = [pd_data['S_secondary'], 0.0] # Creep is eliminated by surcharge
        
        fig_comp = go.Figure(data=[
            go.Bar(name='Tassement Primaire (Consolidation)', x=categories, y=primary_settlement, marker_color='royalblue'),
            go.Bar(name=f'Fluage (sur {design_life} ans)', x=categories, y=secondary_settlement, marker_color='darkorange')
        ])
        
        fig_comp.update_layout(barmode='stack', title="Tassement Projeté (m)", yaxis_title="Tassement cumulé (m)", height=450)
        # Invert Y axis so settlement goes down
        fig_comp.update_yaxes(autorange="reversed")
        st.plotly_chart(fig_comp, use_container_width=True)

    # --- ONGLET 2 : ANALYSE DES RISQUES (NOUVEAU) ---
    with tab_risk:
        st.subheader("Évaluation des Risques Géotechniques Majeurs")
        
        r1, r2 = st.columns(2)
        
        # RISQUE 1 : FLUAGE
        with r1:
            st.markdown("### ⏱️ Risque 1 : Fluage à Long Terme")
            st.info(f"**Tassement Secondaire calculé : {pd_data['S_secondary']:.3f} m** (sur {design_life} ans)")
            if pd_data['S_secondary'] > 0.15:
                st.error("🚨 Le risque de fluage est critique. Le ratio de surcharge actuel n'est peut-être pas suffisant pour 'verrouiller' la compression secondaire des sols organiques.")
            else:
                st.success("✅ Le fluage anticipé est gérable. L'application du ratio de surcharge éliminera ce tassement résiduel.")
                
        # RISQUE 2 : MUDWAVE
        with r2:
            st.markdown("### 🌊 Risque 2 : Rupture au Cisaillement (Mudwave)")
            FS = pd_data['FS_mudwave']
            H_safe = pd_data['H_max_safe']
            
            st.metric("Facteur de Sécurité (FS) Instantané", f"{FS:.2f}", "Risque si FS < 1.3", delta_color="inverse")
            
            if FS < 1.3:
                st.error(f"🚨 **DANGER DE RUPTURE :** La hauteur requise de {pd_data['actual_height']:.1f}m génère une contrainte supérieure à la capacité portante de l'argile molle ($S_u = {pd_data['min_Su']}$ kPa).")
                st.warning(f"🛠️ **Mesure Corrective (Phasage) :** Vous NE POUVEZ PAS poser tout le remblai d'un coup. La hauteur maximale de la première levée ne doit pas dépasser **{H_safe:.2f} m**.")
            else:
                st.success("✅ Le sol en place a une portance suffisante pour recevoir la totalité du remblai en une seule phase sans risque de refoulement.")

    # --- ONGLET 3 : LOGISTIQUE ---
    with tab_log:
        st.subheader("Bilan des Quantités")
        c1, c2, c3 = st.columns(3)
        c1.metric("Surface", f"{pd_data['area_m2']:,.0f} m²")
        c2.metric("Hauteur Remblai (Compensée)", f"{pd_data['actual_height']:.2f} m")
        c3.metric("Volume de Terre", f"{pd_data['volume_m3']:,.0f} m³")
