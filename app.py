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
st.set_page_config(layout="wide", page_title="Port Surcharge Optimizer MASTER")

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
# 2. AUTHENTICATION (CALLBACK)
# ==========================================
def check_password():
    def password_entered():
        if st.session_state["pwd_input"].strip().lower() == "admin":
            st.session_state["authenticated"] = True
            del st.session_state["pwd_input"] 
        else:
            st.session_state["authenticated"] = False

    if not st.session_state.get("authenticated", False):
        st.title("🔒 Accès Sécurisé - Master Géotechnique")
        st.text_input("Code d'accès (tapez 'admin' et Entrée) :", type="password", on_change=password_entered, key="pwd_input")
        if "authenticated" in st.session_state and not st.session_state["authenticated"]:
            st.error("Mot de passe incorrect.")
        st.stop()

check_password()
if st.sidebar.button("Se déconnecter 🚪"):
    st.session_state["authenticated"] = False
    st.rerun()

# ==========================================
# 3. CORE ENGINEERING MATH (ALL MODULES)
# ==========================================
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    base_fill_height = target_load / gamma_fill
    return final_load, target_load, base_fill_height

# --- MODULES DE TASSEMENT MULTICOUCHES ---
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

def calc_settlement_cptu_layered(layers, delta_sigma):
    total_S = 0.0
    for L in layers:
        H, qt, sig_v0, alpha = L['H'], L['qt'], L['sig_v0'], L['alpha']
        M_kPa = alpha * ((qt * 1000) - sig_v0)
        if M_kPa <= 0: M_kPa = 1000 
        total_S += (delta_sigma / M_kPa) * H
    return total_S

def calc_settlement_spt_layered(layers, delta_sigma):
    total_S = 0.0
    for L in layers:
        H, N60, f2 = L['H'], L['N60'], L['f2']
        M_kPa = f2 * N60
        if M_kPa <= 0: M_kPa = 1000
        total_S += (delta_sigma / M_kPa) * H
    return total_S

# --- MODULES DE RISQUES ---
def calc_secondary_compression(layers, t_primary_days, t_design_life_years):
    total_S_sec = 0.0
    t1 = t_primary_days / 365.25
    t2 = t1 + t_design_life_years
    if t1 <= 0 or t2 <= 0: return 0.0
    for L in layers:
        total_S_sec += L['C_alpha'] * L['H'] * math.log10(t2 / t1)
    return total_S_sec

def check_mudwave_risk(Su_min, gamma_fill, H_fill):
    q_ult = 5.14 * Su_min
    q_applied = gamma_fill * H_fill
    FS = q_ult / q_applied if q_applied > 0 else 999
    H_max_safe = q_ult / (gamma_fill * 1.3) # FS visé de 1.3 pour phase provisoire
    return FS, q_ult, q_applied, H_max_safe

# --- MODULES CINÉMATIQUE & SUIVI ---
def hansbo_consolidation(ch_m2_yr, spacing, t_days, apply_smear=False, kh_ks=3.0, ds=0.2):
    if t_days <= 0: return 0.0
    ch_day = ch_m2_yr / 365.25
    D = 1.05 * spacing 
    dw = 0.052 
    n = D / dw
    Fn = math.log(n) - 0.75
    Fs = (kh_ks - 1.0) * math.log(ds / dw) if apply_smear else 0.0
    F = Fn + Fs
    Tr = (ch_day * t_days) / (D**2)
    try: Ur = 1.0 - math.exp((-8.0 * Tr) / F)
    except OverflowError: Ur = 1.0
    return max(0.0, min(Ur, 1.0))

def calculate_asaoka(times, settlements, delta_t=10):
    if len(times) < 3: return None, None, None, None, None
    t_interp = np.arange(min(times), max(times) + delta_t, delta_t)
    s_interp = np.interp(t_interp, times, settlements)
    
    s_n_minus_1 = s_interp[:-1]
    s_n = s_interp[1:]
    
    coef = np.polyfit(s_n_minus_1, s_n, 1)
    beta_1, beta_0 = coef[0], coef[1]
    
    if beta_1 >= 1.0 or beta_1 <= 0: return None, None, None, None, None 
    s_ult = beta_0 / (1 - beta_1)
    return s_ult, beta_0, beta_1, s_n_minus_1, s_n

# ==========================================
# 4. SESSION MEMORY & SIDEBAR
# ==========================================
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

st.sidebar.header("📍 Localisation")
search_query = st.sidebar.text_input("Recherche GPS (Lat, Lon)")
if st.sidebar.button("Aller à"):
    if search_query and "," in search_query:
        try:
            lat, lon = map(float, search_query.split(","))
            st.session_state['map_center'] = [lat, lon]
            st.rerun()
        except: pass

st.sidebar.markdown("---")
st.sidebar.header("🏗️ Charges & Durée de Vie")
dead_load = st.sidebar.number_input("Dead Load (Chaussée) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Exploitation) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input("Poids Volumique Remblai [kN/m³]", value=19.0, step=0.5)
design_life = st.sidebar.number_input("Durée de vie ouvrage [Années]", value=30, step=10)

st.sidebar.markdown("---")
st.sidebar.header("🗜️ Stratigraphie (Sol en place)")
num_layers = st.sidebar.number_input("Nombre de couches", min_value=1, max_value=5, value=1)

soil_layers = []
for i in range(int(num_layers)):
    with st.sidebar.expander(f"Couche {i+1} Paramètres", expanded=(i==0)):
        H = st.number_input(f"Épaisseur (m)", value=8.0, key=f"H_{i}")
        st.caption("Méthode 1: Oedomètre & Fluage")
        e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
        Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
        Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
        sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
        sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
        C_alpha = st.number_input("C_alpha (Fluage)", value=0.015, format="%.3f", key=f"ca_{i}")
        Su = st.number_input("Su (Cohésion) [kPa]", value=15.0, key=f"su_{i}")
        
        st.caption("Méthode 2: CPTu")
        qt = st.number_input("qt [MPa]", value=0.60, key=f"qt_{i}")
        sig_v0 = st.number_input("σv0 (Totale) [kPa]", value=50.0, key=f"sv0_{i}")
        alpha = st.number_input("α_M Factor", value=4.0, key=f"a_{i}")
        
        st.caption("Méthode 3: SPT")
        N60 = st.number_input("N60 Blows", value=3.0, key=f"n60_{i}")
        f2 = st.number_input("f2 Factor [kPa]", value=500.0, key=f"f2_{i}")
        
        soil_layers.append({
            'H': H, 'e0': e0, 'Cc': Cc, 'Cr': Cr, 'sig_0': sig_0, 'sig_c': sig_c, 
            'C_alpha': C_alpha, 'Su': Su, 'qt': qt, 'sig_v0': sig_v0, 'alpha': alpha, 
            'N60': N60, 'f2': f2
        })
total_H = sum(layer['H'] for layer in soil_layers)

st.sidebar.markdown("---")
st.sidebar.header("🚰 Drainage Vertical (PVD)")
ch_val = st.sidebar.number_input("c_h (Couche Critique) [m²/yr]", value=2.0, step=0.5)
pvd_spacing = st.sidebar.slider("Espacement PVD (m)", 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input("Temps Cible (Jours)", value=180, step=10)
apply_smear = st.sidebar.toggle("Inclure Effet 'Smear'", value=True)

# ==========================================
# 5. MAIN UI & CALCULATIONS
# ==========================================
st.title("⚓ Port Terminal Surcharge Optimizer MASTER")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, tiles='CartoDB Positron')
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("1. Délimitation du Pôle")
    output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader("2. Exécution du Modèle")
    if st.button("🚀 CALCULER LE PROJET COMPLET", use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error("🛑 Veuillez dessiner un polygone sur la carte.")
        else:
            with st.spinner("Compilation des modules géotechniques..."):
                poly = Polygon(poly_coords)
                c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
                area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
                
                # 1. Base Loads
                final_load, target_load, base_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
                
                # 2. Settlement Calculations (Tri-Method)
                S_lab_untreated = calc_settlement_oedometer_layered(soil_layers, final_load)
                S_lab_treated = calc_settlement_oedometer_layered(soil_layers, target_load)
                S_cpt = calc_settlement_cptu_layered(soil_layers, target_load)
                S_spt = calc_settlement_spt_layered(soil_layers, target_load)
                
                design_settlement = max(S_lab_treated, S_cpt, S_spt)
                
                # 3. Secondary Compression
                S_secondary = calc_secondary_compression(soil_layers, target_time, design_life)
                
                # 4. Compensated Logistics
                actual_fill_height = base_fill_height + design_settlement
                total_fill_volume = area_m2 * actual_fill_height
                
                # 5. PVD Quantities
                area_per_pvd = 0.866 * (pvd_spacing**2)
                total_pvds_required = math.ceil(area_m2 / area_per_pvd)
                total_pvd_linear_meters = total_pvds_required * total_H

                # 6. Mudwave Risk
                min_Su = min(layer['Su'] for layer in soil_layers)
                FS_mudwave, q_ult, q_applied, H_max_safe = check_mudwave_risk(min_Su, gamma_fill, actual_fill_height)

                st.session_state['project_data'] = {
                    'area_m2': area_m2, 'final_load': final_load, 'target_load': target_load, 
                    'base_height': base_fill_height, 'S_lab_u': S_lab_untreated, 'S_lab_t': S_lab_treated, 
                    'S_cpt': S_cpt, 'S_spt': S_spt, 'S_design': design_settlement, 'S_sec': S_secondary,
                    'actual_height': actual_fill_height, 'volume_m3': total_fill_volume, 
                    'pvd_count': total_pvds_required, 'pvd_length_m': total_pvd_linear_meters, 
                    'FS_mudwave': FS_mudwave, 'H_max_safe': H_max_safe, 'min_Su': min_Su,
                    'polygon': poly_coords
                }
                st.success("✅ Modélisation complète réussie.")

# ==========================================
# 6. RESULTS DASHBOARD (ALL 6 TABS)
# ==========================================
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_risk, tab_geo, tab_log, tab_exec, tab_asaoka, tab_map = st.tabs([
        "⚠️ Tassements & Risques", 
        "📊 Drainage & Consolidation", 
        "🚜 Logistique", 
        "📅 Phasage (Lifts)", 
        "📉 Suivi Asaoka", 
        "🗺️ Carte d'Emprise"
    ])
    
    # --- ONGLET 1 : TASSEMENTS & RISQUES ---
    with tab_risk:
        st.subheader("Bilan Géotechnique & Évaluation des Risques")
        
        c1, c2, c3 = st.columns(3)
        c1.metric("1. Oedomètre", f"{pd_data['S_lab_t']:.3f} m")
        c2.metric("2. CPTu", f"{pd_data['S_cpt']:.3f} m")
        c3.metric("3. SPT", f"{pd_data['S_spt']:.3f} m")
        st.info(f"**Tassement de design (Pire cas retenu) : {pd_data['S_design']:.3f} m**")
        
        st.markdown("---")
        r1, r2 = st.columns(2)
        
        with r1:
            st.markdown("### ⏱️ Tassement Primaire vs Fluage")
            categories = ['SANS Traitement (Définitif)', 'AVEC Traitement (Surcharge)']
            primary_settlement = [pd_data['S_lab_u'], pd_data['S_lab_t']]
            secondary_settlement = [pd_data['S_sec'], 0.0] 
            
            fig_comp = go.Figure(data=[
                go.Bar(name='Primaire', x=categories, y=primary_settlement, marker_color='royalblue'),
                go.Bar(name=f'Fluage ({design_life} ans)', x=categories, y=secondary_settlement, marker_color='darkorange')
            ])
            fig_comp.update_layout(barmode='stack', yaxis_title="Tassement cumulé (m)", height=350, margin=dict(t=30))
            fig_comp.update_yaxes(autorange="reversed")
            st.plotly_chart(fig_comp, use_container_width=True)
            
        with r2:
            st.markdown("### 🌊 Risque de Rupture (Mudwave)")
            FS = pd_data['FS_mudwave']
            st.metric("Facteur de Sécurité Instantané", f"{FS:.2f}", "Risque si FS < 1.3", delta_color="inverse")
            if FS < 1.3:
                st.error(f"🚨 **DANGER :** La hauteur requise ({pd_data['actual_height']:.1f}m) dépasse la capacité portante ($S_u = {pd_data['min_Su']}$ kPa).")
                st.warning(f"🛠️ **Mesure :** Hauteur maximale par levée : **{pd_data['H_max_safe']:.2f} m**.")
            else:
                st.success("✅ Portance suffisante pour une levée unique.")

    # --- ONGLET 2 : CONSOLIDATION HANSBO ---
    with tab_geo:
        st.subheader("Cinématique de Consolidation Radiale (PVD)")
        target_U = hansbo_consolidation(ch_val, pvd_spacing, target_time, apply_smear) * 100
        days_array = np.linspace(0, max(365, int(target_time * 1.5)), 150)
        U_array = [hansbo_consolidation(ch_val, pvd_spacing, t, apply_smear) * 100 for t in days_array]
        
        fig_hansbo = go.Figure()
        fig_hansbo.add_trace(go.Scatter(x=days_array, y=U_array, mode='lines', line=dict(color='royalblue', width=3)))
        fig_hansbo.add_hline(y=90, line_dash="dash", line_color="red", annotation_text="Objectif 90%")
        fig_hansbo.add_vline(x=target_time, line_dash="dot", line_color="green", annotation_text=f"Délai ({target_U:.1f}%)")
        fig_hansbo.update_layout(xaxis_title="Temps (Jours)", yaxis_title="U (%)", yaxis_range=[0, 105], height=400)
        st.plotly_chart(fig_hansbo, use_container_width=True)

    # --- ONGLET 3 : LOGISTIQUE ---
    with tab_log:
        st.subheader("Logistique Compensée (Bilan Matière)")
        st.caption("Intègre le tassement maximum sous le niveau de la plateforme finale.")
        df_logistics = pd.DataFrame({
            "Indicateur": [
                "Emprise au sol (Surface)", "Hauteur de remblai (Théorie)", "Compensation de tassement",
                "Hauteur réelle d'exécution", "Volume de terre à importer", "Nombre d'unités PVD", "Métré linéaire PVD"
            ],
            "Valeur": [
                f"{pd_data['area_m2']:,.0f} m²", f"{pd_data['base_height']:.2f} m", f"+ {pd_data['S_design']:.3f} m",
                f"{pd_data['actual_height']:.2f} m", f"{pd_data['volume_m3']:,.0f} m³", f"{pd_data['pvd_count']:,.0f} drains",
                f"{pd_data['pvd_length_m']:,.0f} ml"
            ]
        })
        st.table(df_logistics)

    # --- ONGLET 4 : PHASAGE (EXECUTION) ---
    with tab_exec:
        st.subheader("Chronogramme de Mise en Œuvre (Lifts)")
        default_lifts = pd.DataFrame({'Jour': [0, 15, 45, 75], 'Hauteur_Ajoutee_m': [0.5, 1.5, 1.5, 1.0], 'Operation': ['Plateforme & PVD', 'Levée 1', 'Levée 2', 'Levée 3']})
        
        col_ed1, col_ed2 = st.columns([1, 2])
        with col_ed1: edited_lifts = st.data_editor(default_lifts, num_rows="dynamic", use_container_width=True)
        with col_ed2:
            edited_lifts = edited_lifts.sort_values(by='Jour')
            edited_lifts['Hauteur_Cumulee_m'] = edited_lifts['Hauteur_Ajoutee_m'].cumsum()
            fig_exec = go.Figure()
            fig_exec.add_trace(go.Scatter(x=edited_lifts['Jour'], y=edited_lifts['Hauteur_Cumulee_m'], mode='lines+markers', line_shape='hv', line=dict(color='orange', width=4), marker=dict(size=10, color='black')))
            fig_exec.add_hline(y=pd_data['actual_height'], line_dash="dash", line_color="red", annotation_text=f"Objectif ({pd_data['actual_height']:.2f} m)")
            fig_exec.update_layout(xaxis_title="Jours", yaxis_title="Hauteur (m)", height=400)
            st.plotly_chart(fig_exec, use_container_width=True)

    # --- ONGLET 5 : ASAOKA (SUIVI) ---
    with tab_asaoka:
        st.subheader("Méthode Observationnelle d'Asaoka")
        default_monitor = pd.DataFrame({'Jour': [0, 15, 30, 45, 60, 75, 90, 105], 'Tassement_m': [0.0, 0.12, 0.22, 0.30, 0.36, 0.41, 0.45, 0.48]})
        
        col_as1, col_as2 = st.columns([1, 2])
        with col_as1:
            delta_t = st.number_input("Intervalle (Δt) Asaoka [Jours]", value=15, step=5)
            edited_monitor = st.data_editor(default_monitor, num_rows="dynamic", use_container_width=True).sort_values(by='Jour')
            times, settlements = edited_monitor['Jour'].values, edited_monitor['Tassement_m'].values
            s_ult_asaoka, b0, b1, sn_minus_1, sn = calculate_asaoka(times, settlements, delta_t)
            
            if s_ult_asaoka:
                st.success(f"**$S_{{ult}}$ Projeté : {s_ult_asaoka:.3f} m**")
                consolidation_pct = (settlements[-1] / s_ult_asaoka) * 100
                st.info(f"Consolidation Actuelle : **{consolidation_pct:.1f}%**")
        
        with col_as2:
            if s_ult_asaoka:
                fig_asaoka = make_subplots(rows=1, cols=2, subplot_titles=("Relevés Terrain", "Graphe d'Asaoka"))
                fig_asaoka.add_trace(go.Scatter(x=times, y=settlements, mode='markers+lines', line=dict(color='blue', width=2)), row=1, col=1)
                fig_asaoka.add_hline(y=s_ult_asaoka, line_dash="dash", line_color="red", row=1, col=1)
                fig_asaoka.add_trace(go.Scatter(x=sn_minus_1, y=sn, mode='markers', marker=dict(color='black')), row=1, col=2)
                x_trend = np.array([0, s_ult_asaoka * 1.1])
                fig_asaoka.add_trace(go.Scatter(x=x_trend, y=b0 + b1 * x_trend, mode='lines', line=dict(color='orange', dash='dash')), row=1, col=2)
                fig_asaoka.add_trace(go.Scatter(x=x_trend, y=x_trend, mode='lines', line=dict(color='green')), row=1, col=2)
                fig_asaoka.update_layout(height=400, showlegend=False)
                st.plotly_chart(fig_asaoka, use_container_width=True)

    # --- ONGLET 6 : CARTE ---
    with tab_map:
        st.subheader("Emprise du Traitement")
        c_lat_res = sum([p[1] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        c_lon_res = sum([p[0] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        m_res = folium.Map(location=[c_lat_res, c_lon_res], zoom_start=16, tiles='CartoDB Positron')
        folium.Polygon(locations=[(p[1], p[0]) for p in pd_data['polygon']], color='orange', weight=3, fill=True, fill_opacity=0.4).add_to(m_res)
        st_folium(m_res, width=1200, height=500, key="result_map")
