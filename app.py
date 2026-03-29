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
# 2. AUTHENTICATION (CALLBACK PATTERN)
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
            st.error("Mot de passe incorrect. Veuillez réessayer.")
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

# --- NEW: ASAOKA METHOD CALCULATION ---
def calculate_asaoka(times, settlements, delta_t=10):
    if len(times) < 3:
        return None, None, None, None, None
    
    # Interpolate to strictly constant time intervals (delta_t)
    t_interp = np.arange(min(times), max(times) + delta_t, delta_t)
    s_interp = np.interp(t_interp, times, settlements)
    
    s_n_minus_1 = s_interp[:-1]
    s_n = s_interp[1:]
    
    # Linear Regression: S_n = beta_0 + beta_1 * S_{n-1}
    coef = np.polyfit(s_n_minus_1, s_n, 1)
    beta_1, beta_0 = coef[0], coef[1]
    
    if beta_1 >= 1.0 or beta_1 <= 0:
        return None, None, None, None, None # Invalid consolidation curve
        
    s_ult = beta_0 / (1 - beta_1)
    return s_ult, beta_0, beta_1, s_n_minus_1, s_n

# ==========================================
# 4. SESSION STATE MEMORY
# ==========================================
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# ==========================================
# 5. SIDEBAR PARAMETERS
# ==========================================
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
st.sidebar.header("🏗️ Charges (Loading)")
dead_load = st.sidebar.number_input("Dead Load (Chaussée) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Exploitation) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input("Poids Volumique Remblai (γ) [kN/m³]", value=19.0, step=0.5)

st.sidebar.markdown("---")
st.sidebar.header("🗜️ Stratigraphie (Zoning)")
num_layers = st.sidebar.number_input("Nombre de couches compressibles", min_value=1, max_value=5, value=1)

soil_layers = []
for i in range(int(num_layers)):
    with st.sidebar.expander(f"Couche {i+1} Parameters", expanded=(i==0)):
        H = st.number_input(f"Épaisseur (m)", value=8.0, key=f"H_{i}")
        e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
        Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
        Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
        sig_0 = st.number_input("σ'0 [kPa]", value=40.0, key=f"s0_{i}")
        sig_c = st.number_input("σ'c [kPa]", value=45.0, key=f"sc_{i}")
        qt = st.number_input("qt [MPa]", value=0.60, key=f"qt_{i}")
        sig_v0 = st.number_input("σv0 (Totale) [kPa]", value=50.0, key=f"sv0_{i}")
        alpha = st.number_input("α_M Factor", value=4.0, key=f"a_{i}")
        N60 = st.number_input("N60 Blows", value=3.0, key=f"n60_{i}")
        f2 = st.number_input("f2 Factor [kPa]", value=500.0, key=f"f2_{i}")
        
        soil_layers.append({
            'H': H, 'e0': e0, 'Cc': Cc, 'Cr': Cr, 'sig_0': sig_0, 'sig_c': sig_c,
            'qt': qt, 'sig_v0': sig_v0, 'alpha': alpha, 'N60': N60, 'f2': f2
        })
total_H = sum(layer['H'] for layer in soil_layers)

st.sidebar.markdown("---")
st.sidebar.header("🚰 Drainage Vertical (PVD)")
ch_val = st.sidebar.number_input("c_h (Couche Critique) [m²/yr]", value=2.0, step=0.5)
pvd_spacing = st.sidebar.slider("Espacement PVD (m)", 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input("Temps Cible (Jours)", value=180, step=10)
apply_smear = st.sidebar.toggle("Inclure Effet 'Smear'", value=True)

# ==========================================
# 6. MAIN UI & CALCULATIONS
# ==========================================
st.title("⚓ Port Terminal Surcharge Optimizer PRO")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, 
               tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri')
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("1. Délimitation du Pôle")
    output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader("2. Exécution du Modèle")
    if st.button("🚀 CALCULER LE PROJET", use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error("🛑 Veuillez dessiner un polygone sur la carte.")
        else:
            poly = Polygon(poly_coords)
            c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
            area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
            
            final_load, target_load, base_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
            S_lab = calc_settlement_oedometer_layered(soil_layers, target_load)
            S_cpt = calc_settlement_cptu_layered(soil_layers, target_load)
            S_spt = calc_settlement_spt_layered(soil_layers, target_load)
            
            design_settlement = max(S_lab, S_cpt, S_spt)
            actual_fill_height = base_fill_height + design_settlement
            total_fill_volume = area_m2 * actual_fill_height
            
            st.session_state['project_data'] = {
                'area_m2': area_m2, 'target_load': target_load, 'base_height': base_fill_height,
                'S_lab': S_lab, 'S_cpt': S_cpt, 'S_spt': S_spt, 'S_design': design_settlement,
                'actual_height': actual_fill_height, 'volume_m3': total_fill_volume, 
                'total_H': total_H, 'polygon': poly_coords
            }
            st.success("✅ Modélisation terminée.")

# ==========================================
# 7. RESULTS DASHBOARD (WITH PHASES 3, 4, 5)
# ==========================================
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_set, tab_exec, tab_asaoka = st.tabs([
        "🗜️ Tassements & Volume (Phases 1-2)", 
        "📅 Phasage & Exécution (Phases 3-4)", 
        "📉 Suivi Asaoka (Phase 5)"
    ])
    
    # --- TAB 1: EXISTING RESULTS ---
    with tab_set:
        st.subheader("Prédictions Théoriques & Logistique")
        c1, c2, c3 = st.columns(3)
        c1.metric("1. Oedomètre", f"{pd_data['S_lab']:.3f} m")
        c2.metric("2. CPTu", f"{pd_data['S_cpt']:.3f} m")
        c3.metric("Hauteur Remblai Requise", f"{pd_data['actual_height']:.2f} m", f"Inclus {pd_data['S_design']:.2f}m de tassement")
        st.info(f"Volume total compensé à importer : **{pd_data['volume_m3']:,.0f} m³**")

    # --- TAB 2: EXECUTION TRACKING (Phases 3 & 4) ---
    with tab_exec:
        st.subheader("Chronogramme de Mise en Œuvre (Lifts)")
        st.caption("Modifiez le tableau ci-dessous pour planifier ou enregistrer les dates de pose des PVD et des levées de terre.")
        
        # Default Execution Data
        default_lifts = pd.DataFrame({
            'Jour': [0, 15, 45, 75],
            'Hauteur_Ajoutee_m': [0.5, 1.5, 1.5, 1.0],
            'Operation': ['Plateforme Sable & PVD', 'Levée 1', 'Levée 2', 'Levée 3 (Cible atteinte)']
        })
        
        col_ed1, col_ed2 = st.columns([1, 2])
        with col_ed1:
            edited_lifts = st.data_editor(default_lifts, num_rows="dynamic", use_container_width=True)
            
        with col_ed2:
            # Calculate cumulative height for step chart
            edited_lifts = edited_lifts.sort_values(by='Jour')
            edited_lifts['Hauteur_Cumulee_m'] = edited_lifts['Hauteur_Ajoutee_m'].cumsum()
            
            fig_exec = go.Figure()
            # line_shape='hv' creates the horizontal-vertical step chart typical of fill placement
            fig_exec.add_trace(go.Scatter(x=edited_lifts['Jour'], y=edited_lifts['Hauteur_Cumulee_m'], 
                                          mode='lines+markers', line_shape='hv', name='Hauteur de Remblai',
                                          line=dict(color='orange', width=4), marker=dict(size=10, color='black')))
            
            # Add target line
            fig_exec.add_hline(y=pd_data['actual_height'], line_dash="dash", line_color="red", 
                               annotation_text=f"Objectif Final ({pd_data['actual_height']:.2f} m)")
            
            fig_exec.update_layout(title="Progression du Chargement (Surcharge Lifts)", xaxis_title="Temps (Jours)", 
                                   yaxis_title="Épaisseur du Remblai (m)", height=400)
            st.plotly_chart(fig_exec, use_container_width=True)

    # --- TAB 3: ASAOKA OBSERVATIONAL METHOD (Phase 5) ---
    with tab_asaoka:
        st.subheader("Méthode Observationnelle d'Asaoka")
        st.caption("Saisissez les relevés des plaques de tassement sur le terrain. L'IA corrigera les intervalles et projettera le tassement ultime réel.")
        
        # Default Monitoring Data (Showing typical consolidation curve)
        default_monitor = pd.DataFrame({
            'Jour': [0, 15, 30, 45, 60, 75, 90, 105],
            'Tassement_m': [0.0, 0.12, 0.22, 0.30, 0.36, 0.41, 0.45, 0.48]
        })
        
        col_as1, col_as2 = st.columns([1, 2])
        
        with col_as1:
            delta_t = st.number_input("Intervalle de temps (Δt) pour Asaoka [Jours]", value=15, step=5)
            edited_monitor = st.data_editor(default_monitor, num_rows="dynamic", use_container_width=True)
            edited_monitor = edited_monitor.sort_values(by='Jour')
            
            times = edited_monitor['Jour'].values
            settlements = edited_monitor['Tassement_m'].values
            
            s_ult_asaoka, b0, b1, sn_minus_1, sn = calculate_asaoka(times, settlements, delta_t)
            
            if s_ult_asaoka:
                st.success(f"**Tassement Ultime Projeté ($S_{{ult}}$) : {s_ult_asaoka:.3f} m**")
                consolidation_pct = (settlements[-1] / s_ult_asaoka) * 100
                st.info(f"Consolidation actuelle : **{consolidation_pct:.1f}%**")
                if consolidation_pct >= 90.0:
                    st.balloons()
                    st.success("✅ **Objectif de 90% atteint ! Autorisation de déchargement.**")
            else:
                st.error("Données insuffisantes ou non-convergentes pour la méthode d'Asaoka.")

        with col_as2:
            if s_ult_asaoka:
                fig_asaoka = make_subplots(rows=1, cols=2, subplot_titles=("Données de Terrain vs Projection", "Graphe d'Asaoka ($S_n$ vs $S_{n-1}$)"))
                
                # Plot 1: Standard Time-Settlement Curve
                fig_asaoka.add_trace(go.Scatter(x=times, y=settlements, mode='markers+lines', name='Relevés Terrain', 
                                                line=dict(color='blue', width=2), marker=dict(size=8)), row=1, col=1)
                fig_asaoka.add_hline(y=s_ult_asaoka, line_dash="dash", line_color="red", 
                                     annotation_text=f"Asaoka S_ult ({s_ult_asaoka:.2f}m)", row=1, col=1)
                
                # Plot 2: Asaoka Plot
                fig_asaoka.add_trace(go.Scatter(x=sn_minus_1, y=sn, mode='markers', name='Points Asaoka', marker=dict(color='black', size=8)), row=1, col=2)
                
                # Trendline
                x_trend = np.array([0, s_ult_asaoka * 1.1])
                y_trend = b0 + b1 * x_trend
                fig_asaoka.add_trace(go.Scatter(x=x_trend, y=y_trend, mode='lines', name='Tendance Linéaire (Ajustement)', 
                                                line=dict(color='orange', width=2, dash='dash')), row=1, col=2)
                
                # 45-degree line (y = x)
                fig_asaoka.add_trace(go.Scatter(x=x_trend, y=x_trend, mode='lines', name='Ligne 45° ($S_n = S_{n-1}$)', 
                                                line=dict(color='green', width=1)), row=1, col=2)
                
                fig_asaoka.update_layout(height=450, showlegend=False)
                fig_asaoka.update_xaxes(title_text="Temps (Jours)", row=1, col=1)
                fig_asaoka.update_yaxes(title_text="Tassement (m)", row=1, col=1)
                fig_asaoka.update_xaxes(title_text="Tassement S_{n-1} (m)", row=1, col=2)
                fig_asaoka.update_yaxes(title_text="Tassement S_n (m)", row=1, col=2)
                
                st.plotly_chart(fig_asaoka, use_container_width=True)

# ==========================================
# END OF SCRIPT
# ==========================================
