import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import pandas as pd
import numpy as np
from shapely.geometry import Polygon
import math
import plotly.graph_objects as go

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
if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if not st.session_state["authenticated"]:
    st.title("🔒 Accès Sécurisé - Simulateur Portuaire")
    with st.form("login_form"):
        if st.form_submit_button("Se connecter") and st.text_input("Code d'accès :", type="password") in st.secrets.get("passwords", {"default": "admin"}).values():
            st.session_state["authenticated"] = True
            st.rerun() 
        else: st.info("Entrez le mot de passe (par défaut: admin)")
    st.stop()

if st.sidebar.button("Se déconnecter 🚪"): st.session_state["authenticated"] = False; st.rerun()

# --- MEMOIRE DE SESSION --- 
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698] 
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# ==========================================
# 3. CORE ENGINEERING MATH (UPGRADED FOR LAYERS)
# ==========================================
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    base_fill_height = target_load / gamma_fill
    return final_load, target_load, base_fill_height

def calc_settlement_oedometer_layered(layers, delta_sigma):
    """Calculates S_ult by summing the discrete settlement of multiple strata."""
    total_S = 0.0
    for L in layers:
        H, e0, Cc, Cr, sig_0, sig_c = L['H'], L['e0'], L['Cc'], L['Cr'], L['sig_0'], L['sig_c']
        # Simplified Boussinesq: Assuming 1D wide-fill, delta_sigma is relatively constant.
        # For deep layers or small fills, an attenuation factor (I_z) should be applied here.
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
        if M_kPa <= 0: M_kPa = 1000 # Failsafe for mud
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

# ==========================================
# 4. SIDEBAR PARAMETERS (DYNAMIC ZONING)
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
num_layers = st.sidebar.number_input("Nombre de couches compressibles", min_value=1, max_value=5, value=2)

soil_layers = []
for i in range(int(num_layers)):
    with st.sidebar.expander(f"Couche {i+1} Parameters", expanded=(i==0)):
        st.caption("Dimensions & Oedometer")
        H = st.number_input(f"Épaisseur (m)", value=5.0, key=f"H_{i}")
        e0 = st.number_input("e0", value=1.20, key=f"e0_{i}")
        Cc = st.number_input("Cc", value=0.45, key=f"Cc_{i}")
        Cr = st.number_input("Cr", value=0.05, key=f"Cr_{i}")
        sig_0 = st.number_input("σ'0 [kPa]", value=40.0+(i*20), key=f"s0_{i}")
        sig_c = st.number_input("σ'c [kPa]", value=45.0+(i*20), key=f"sc_{i}")
        
        st.caption("CPTu & SPT")
        qt = st.number_input("qt [MPa]", value=0.60, key=f"qt_{i}")
        sig_v0 = st.number_input("σv0 (Totale) [kPa]", value=50.0+(i*20), key=f"sv0_{i}")
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
# 5. MAIN UI & CALCULATIONS
# ==========================================
st.title("⚓ Port Terminal Surcharge Optimizer PRO")

with st.expander("⚠️ Notes Techniques & Limites du Modèle (Geotechnical Disclaimers)"):
    st.warning("""
    * **Diffusion des Contraintes (Boussinesq) :** Le modèle suppose une charge $\Delta\sigma$ constante sur toute la profondeur. Cela est valide pour le centre d'une très large zone de remblai, mais surestime le tassement sur les bords.
    * **Fluage (Secondary Compression) :** Le fluage à long terme ($C_\\alpha$) n'est pas modélisé. Le ratio de surcharge doit être calculé par l'ingénieur pour anticiper et annuler cette phase.
    * **Nappes Phréatiques :** Les contraintes effectives ($\sigma'_0$) saisies supposent un niveau d'eau statique. Un rabattement de nappe augmentera instantanément le tassement.
    * **Risque de Rupture (Mudwave) :** L'application de la hauteur totale de remblai requise doit se faire par levées progressives pour éviter une rupture au cisaillement non drainé ($S_u$).
    """)

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
            with st.spinner("Analyse Stratigraphique en cours..."):
                poly = Polygon(poly_coords)
                c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
                area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
                
                # Loads
                final_load, target_load, base_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
                
                # Multi-Layer Settlement
                S_lab = calc_settlement_oedometer_layered(soil_layers, target_load)
                S_cpt = calc_settlement_cptu_layered(soil_layers, target_load)
                S_spt = calc_settlement_spt_layered(soil_layers, target_load)
                
                # Always design for the worst-case envelope
                design_settlement = max(S_lab, S_cpt, S_spt)
                
                # Compensated Logistics
                actual_fill_height = base_fill_height + design_settlement
                total_fill_volume = area_m2 * actual_fill_height
                total_fill_tonnage = (total_fill_volume * gamma_fill) / 9.81
                
                area_per_pvd = 0.866 * (pvd_spacing**2)
                total_pvds_required = math.ceil(area_m2 / area_per_pvd)
                total_pvd_linear_meters = total_pvds_required * total_H

                st.session_state['project_data'] = {
                    'area_m2': area_m2, 'target_load': target_load, 'base_height': base_fill_height,
                    'S_lab': S_lab, 'S_cpt': S_cpt, 'S_spt': S_spt, 'S_design': design_settlement,
                    'actual_height': actual_fill_height, 'volume_m3': total_fill_volume, 
                    'tonnage_t': total_fill_tonnage, 'pvd_count': total_pvds_required, 
                    'pvd_length_m': total_pvd_linear_meters, 'polygon': poly_coords, 'total_H': total_H
                }
                st.success("✅ Modélisation terminée.")

# ==========================================
# 6. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_set, tab_geo, tab_log, tab_map = st.tabs([
        "🗜️ Tassements (Tri-Méthode)", 
        "📊 Cinématique de Consolidation", 
        "🚜 Logistique & Terrassement", 
        "🗺️ Plan d'Exécution"
    ])
    
    with tab_set:
        st.subheader("Prédictions de Tassement Cumulé (Modèle Multicouche)")
        st.caption(f"Intégration sur {num_layers} couches (Profondeur totale: {pd_data['total_H']}m) sous charge cible de {pd_data['target_load']:.0f} kPa.")
        
        c1, c2, c3 = st.columns(3)
        c1.metric("1. Oedomètre (Labo)", f"{pd_data['S_lab']:.3f} m")
        c2.metric("2. CPTu (Modèle In-Situ)", f"{pd_data['S_cpt']:.3f} m")
        c3.metric("3. SPT (Approximation)", f"{pd_data['S_spt']:.3f} m")
        
        st.info(f"💡 **Verdict Géotechnique :** L'enveloppe de sécurité maximale retenue pour le volume de remblai est de **{pd_data['S_design']:.3f} m**.")

    with tab_geo:
        st.subheader("Courbe de Consolidation Radiale (Théorie de Hansbo)")
        target_U = hansbo_consolidation(ch_val, pvd_spacing, target_time, apply_smear) * 100
        
        days_array = np.linspace(0, max(365, int(target_time * 1.5)), 150)
        U_array = [hansbo_consolidation(ch_val, pvd_spacing, t, apply_smear) * 100 for t in days_array]
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=days_array, y=U_array, mode='lines', name=f'Espacement {pvd_spacing}m', line=dict(color='royalblue', width=3)))
        fig.add_hline(y=90, line_dash="dash", line_color="red", annotation_text="Objectif 90%")
        fig.add_vline(x=target_time, line_dash="dot", line_color="green", annotation_text=f"Délai ({target_U:.1f}%)")
        
        fig.update_layout(xaxis_title="Temps (Jours)", yaxis_title="Degré de Consolidation U (%)", yaxis_range=[0, 105], height=400, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    with tab_log:
        st.subheader("Quantitatifs Corrigés (Bilan Matière)")
        st.caption("Le volume compense automatiquement la fraction de remblai qui s'enfoncera sous la surface finale (Compensation de tassement).")
        
        df_logistics = pd.DataFrame({
            "Indicateur": [
                "Emprise au sol (Surface)", 
                "Hauteur de remblai stricte (Théorie)",
                "Majoration pour tassement (Compensation)",
                "Hauteur réelle d'exécution (Levées totales)",
                "Volume total de terre à importer", 
                "Tonnage approximatif (Logistique camions)",
                "Nombre d'unités PVD", 
                "Métré linéaire total PVD (Achat)"
            ],
            "Valeur": [
                f"{pd_data['area_m2']:,.0f} m²",
                f"{pd_data['base_height']:.2f} m",
                f"+ {pd_data['S_design']:.3f} m",
                f"{pd_data['actual_height']:.2f} m",
                f"{pd_data['volume_m3']:,.0f} m³",
                f"{pd_data['tonnage_t']:,.0f} Tonnes",
                f"{pd_data['pvd_count']:,.0f} drains",
                f"{pd_data['pvd_length_m']:,.0f} ml"
            ]
        })
        st.table(df_logistics)

    with tab_map:
        st.subheader("Emprise du Traitement de Sol")
        c_lat_res = sum([p[1] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        c_lon_res = sum([p[0] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        
        m_res = folium.Map(location=[c_lat_res, c_lon_res], zoom_start=16, tiles='CartoDB Positron')
        folium.Polygon(
            locations=[(p[1], p[0]) for p in pd_data['polygon']], 
            color='orange', weight=3, fill=True, fill_opacity=0.4,
            tooltip=f"Volume à purger/remblayer : {pd_data['volume_m3']:,.0f} m³"
        ).add_to(m_res)
        
        st_folium(m_res, width=1200, height=500, key="result_map")
