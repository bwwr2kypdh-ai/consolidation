import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import requests
import pandas as pd
import numpy as np
from shapely.geometry import Polygon
import math
import plotly.graph_objects as go

# ==========================================
# 1. PAGE CONFIGURATION & CSS
# ==========================================
st.set_page_config(layout="wide", page_title="Port Surcharge Optimizer")

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
if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if not st.session_state["authenticated"]:
    st.title("🔒 Accès Sécurisé - Simulateur Portuaire")
    with st.form("login_form"):
        if st.form_submit_button("Se connecter") and st.text_input("Code d'accès :", type="password") in st.secrets.get("passwords", {"default": "admin"}).values():
            st.session_state["authenticated"] = True
            st.rerun() 
        else: st.info("Entrez le mot de passe (par défaut: admin)")
    st.stop()

st.title("")
if st.sidebar.button("Se déconnecter 🚪"): st.session_state["authenticated"] = False; st.rerun()

# --- MEMOIRE DE SESSION --- 
if 'raw_df' not in st.session_state: 
    st.session_state['raw_df'] = None 
if 'geoms' not in st.session_state: 
    st.session_state['geoms'] = {'poly': None} 
if 'map_center' not in st.session_state: 
    st.session_state['map_center'] = [43.2965, 5.3698] 
if 'last_buffer' not in st.session_state: 
    st.session_state['last_buffer'] = 50 
if 'rect_data' not in st.session_state:
    st.session_state['rect_data'] = {'coords': [], 'area': 0.0, 'type': 'Rectangle'}

# ==========================================
# 3. CORE ENGINEERING MATH
# ==========================================
def calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill):
    final_load = dead_load + live_load
    target_load = final_load * surcharge_ratio
    base_fill_height = target_load / gamma_fill
    return final_load, target_load, base_fill_height

def calc_settlement_oedometer(H, e0, Cc, Cr, sigma_0_prime, sigma_c_prime, delta_sigma):
    final_stress = sigma_0_prime + delta_sigma
    if final_stress <= sigma_c_prime:
        return H * (Cr / (1 + e0)) * math.log10(final_stress / sigma_0_prime)
    else:
        S_recomp = H * (Cr / (1 + e0)) * math.log10(sigma_c_prime / sigma_0_prime)
        S_virgin = H * (Cc / (1 + e0)) * math.log10(final_stress / sigma_c_prime)
        return S_recomp + S_virgin

def calc_settlement_cptu(H, qt_MPa, sigma_v0_kPa, alpha_M, delta_sigma_kPa):
    qt_kPa = qt_MPa * 1000 
    M_kPa = alpha_M * (qt_kPa - sigma_v0_kPa)
    if M_kPa <= 0: M_kPa = 1000
    return (delta_sigma_kPa / M_kPa) * H

def calc_settlement_spt(H, N60, f2_factor_kPa, delta_sigma_kPa):
    M_kPa = f2_factor_kPa * N60
    if M_kPa <= 0: M_kPa = 1000
    return (delta_sigma_kPa / M_kPa) * H

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
# 4. SESSION STATE MEMORY
# ==========================================
if 'map_center' not in st.session_state: st.session_state['map_center'] = [43.2965, 5.3698]
if 'project_data' not in st.session_state: st.session_state['project_data'] = None

# ==========================================
# 5. SIDEBAR PARAMETERS
# ==========================================
st.sidebar.header("📍 Site Location")
search_query = st.sidebar.text_input("Search Coordinates (Lat, Lon)")
if st.sidebar.button("Go to Location"):
    if search_query and "," in search_query:
        try:
            lat, lon = map(float, search_query.split(","))
            st.session_state['map_center'] = [lat, lon]
            st.rerun()
        except: pass

st.sidebar.markdown("---")
st.sidebar.header("🏗️ Structural Loading")
dead_load = st.sidebar.number_input("Dead Load (Pavement) [kPa]", value=20.0, step=5.0)
live_load = st.sidebar.number_input("Live Load (Operations) [kPa]", value=80.0, step=5.0)
surcharge_ratio = st.sidebar.slider("Surcharge Ratio", 1.0, 2.0, 1.2, step=0.05)
gamma_fill = st.sidebar.number_input("Fill Unit Weight (γ) [kN/m³]", value=19.0, step=0.5)

st.sidebar.markdown("---")
st.sidebar.header("🗜️ Soil Compressibility")
H_layer = st.sidebar.number_input("Compressible Layer Thickness (m)", value=10.0, step=1.0)

with st.sidebar.expander("1. Oedometer (Lab)", expanded=True):
    e0 = st.number_input("e0", value=1.20)
    Cc = st.number_input("Cc", value=0.45)
    Cr = st.number_input("Cr", value=0.05)
    sig_0 = st.number_input("σ'0 [kPa]", value=40.0)
    sig_c = st.number_input("σ'c [kPa]", value=45.0)

with st.sidebar.expander("2. CPTu (Field)"):
    qt = st.number_input("qt [MPa]", value=0.60)
    sig_v0 = st.number_input("σv0 [kPa]", value=50.0)
    alpha_M = st.number_input("α_M Factor", value=4.0)

with st.sidebar.expander("3. SPT (Field)"):
    N60 = st.number_input("N60 Blows", value=3.0)
    f2 = st.number_input("f2 Factor [kPa]", value=500.0)

st.sidebar.markdown("---")
st.sidebar.header("🚰 PVD Drain Design")
ch_val = st.sidebar.number_input("Radial Cons. (ch) [m²/yr]", value=2.0, step=0.5)
pvd_spacing = st.sidebar.slider("PVD Spacing (m)", 0.8, 3.0, 1.2, step=0.1)
target_time = st.sidebar.number_input("Target Time (Days)", value=180, step=10)
apply_smear = st.sidebar.toggle("Include Smear Effect", value=True)

# ==========================================
# 6. MAIN UI & MAP SELECTION
# ==========================================
st.title("⚓ Port Terminal Surcharge Optimizer")

m = folium.Map(location=st.session_state['map_center'], zoom_start=15, 
               tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
               attr='Esri')
Draw(export=True, draw_options={'polyline':False, 'polygon':True, 'rectangle':True, 'circle':False, 'marker':False}).add_to(m)

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("1. Define Treatment Area")
    output = st_folium(m, width=800, height=500, key="input_map")

with col2:
    st.subheader("2. Execute Design")
    if st.button("🚀 CALCULATE SURCHARGE", use_container_width=True, type="primary"):
        poly_coords = None
        if output and output.get("all_drawings"):
            polys = [d for d in output["all_drawings"] if d.get("geometry", {}).get("type") == "Polygon"]
            if polys: poly_coords = polys[-1]["geometry"]["coordinates"][0]
        
        if not poly_coords:
            st.error("🛑 Please draw a polygon on the map first.")
        else:
            with st.spinner("Processing Geotechnical Models..."):
                poly = Polygon(poly_coords)
                c_lat = (poly.bounds[1] + poly.bounds[3]) / 2
                area_m2 = poly.area * (111000**2) * math.cos(math.radians(c_lat))
                
                # 1. Base Loads
                final_load, target_load, base_fill_height = calculate_loads(dead_load, live_load, surcharge_ratio, gamma_fill)
                
                # 2. Settlement Calculations
                S_lab = calc_settlement_oedometer(H_layer, e0, Cc, Cr, sig_0, sig_c, target_load)
                S_cpt = calc_settlement_cptu(H_layer, qt, sig_v0, alpha_M, target_load)
                S_spt = calc_settlement_spt(H_layer, N60, f2, target_load)
                
                design_settlement = max(S_lab, S_cpt, S_spt)
                
                # 3. Compensated Logistics
                actual_fill_height = base_fill_height + design_settlement
                total_fill_volume = area_m2 * actual_fill_height
                total_fill_tonnage = (total_fill_volume * gamma_fill) / 9.81
                
                # 4. PVD Logistics
                area_per_pvd = 0.866 * (pvd_spacing**2)
                total_pvds_required = math.ceil(area_m2 / area_per_pvd)
                total_pvd_linear_meters = total_pvds_required * H_layer

                # Save State
                st.session_state['project_data'] = {
                    'area_m2': area_m2,
                    'target_load': target_load,
                    'base_height': base_fill_height,
                    'S_lab': S_lab, 'S_cpt': S_cpt, 'S_spt': S_spt, 'S_design': design_settlement,
                    'actual_height': actual_fill_height,
                    'volume_m3': total_fill_volume, 'tonnage_t': total_fill_tonnage,
                    'pvd_count': total_pvds_required, 'pvd_length_m': total_pvd_linear_meters,
                    'polygon': poly_coords
                }
                st.success("✅ Calculations Complete!")

# ==========================================
# 7. RESULTS DASHBOARD
# ==========================================
if st.session_state['project_data'] is not None:
    pd_data = st.session_state['project_data']
    
    st.markdown("---")
    tab_set, tab_geo, tab_log, tab_map = st.tabs([
        "🗜️ Tri-Method Settlement", 
        "📊 Consolidation Dynamics", 
        "🚜 Earthworks Logistics", 
        "🗺️ Topography Map"
    ])
    
    # TAB 1: SETTLEMENT
    with tab_set:
        st.subheader("Compressibility Predictions")
        st.caption(f"Predicted settlement under the Target Surcharge Load of {pd_data['target_load']:.0f} kPa.")
        
        c1, c2, c3 = st.columns(3)
        c1.metric("1. Oedometer (Lab)", f"{pd_data['S_lab']:.3f} m")
        c2.metric("2. CPTu (Field)", f"{pd_data['S_cpt']:.3f} m")
        c3.metric("3. SPT (Field)", f"{pd_data['S_spt']:.3f} m")
        
        st.info(f"💡 **Design Protocol:** The system has selected the maximum envelope of **{pd_data['S_design']:.3f} m** to safely calculate volumetric fill requirements.")

    # TAB 2: CONSOLIDATION GRAPH
    with tab_geo:
        st.subheader("Time-Consolidation Analysis (Hansbo)")
        target_U = hansbo_consolidation(ch_val, pvd_spacing, target_time, apply_smear) * 100
        
        days_array = np.linspace(0, max(365, int(target_time * 1.5)), 150)
        U_array = [hansbo_consolidation(ch_val, pvd_spacing, t, apply_smear) * 100 for t in days_array]
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=days_array, y=U_array, mode='lines', name=f'Spacing {pvd_spacing}m', line=dict(color='royalblue', width=3)))
        fig.add_hline(y=90, line_dash="dash", line_color="red", annotation_text="90% Requirement")
        fig.add_vline(x=target_time, line_dash="dot", line_color="green", annotation_text=f"Target Deadline ({target_U:.1f}%)")
        
        fig.update_layout(xaxis_title="Time (Days)", yaxis_title="Degree of Consolidation U (%)", yaxis_range=[0, 105], height=400, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    # TAB 3: LOGISTICS
    with tab_log:
        st.subheader("Compensated Earthworks Quantities")
        st.caption("Calculations automatically account for the volume of dirt that sinks below surface level during settlement.")
        
        df_logistics = pd.DataFrame({
            "Metric": [
                "Treatment Area Footprint", 
                "Base Fill Height (Theoretical)",
                "Settlement Compensation Added",
                "Actual Fill Height Required",
                "Total Temporary Fill Volume", 
                "Total Fill Tonnage (Approx)",
                "Total PVDs Required", 
                "Total Linear Meters of PVD"
            ],
            "Value": [
                f"{pd_data['area_m2']:,.0f} m²",
                f"{pd_data['base_height']:.2f} m",
                f"+ {pd_data['S_design']:.3f} m",
                f"{pd_data['actual_height']:.2f} m",
                f"{pd_data['volume_m3']:,.0f} m³",
                f"{pd_data['tonnage_t']:,.0f} Tons",
                f"{pd_data['pvd_count']:,.0f} drains",
                f"{pd_data['pvd_length_m']:,.0f} linear m"
            ]
        })
        st.table(df_logistics)

    # TAB 4: MAP
    with tab_map:
        st.subheader("Treatment Footprint")
        c_lat_res = sum([p[1] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        c_lon_res = sum([p[0] for p in pd_data['polygon']]) / len(pd_data['polygon'])
        
        m_res = folium.Map(location=[c_lat_res, c_lon_res], zoom_start=16, tiles='CartoDB Positron')
        folium.Polygon(
            locations=[(p[1], p[0]) for p in pd_data['polygon']], 
            color='orange', weight=3, fill=True, fill_opacity=0.4,
            tooltip=f"Volume Required: {pd_data['volume_m3']:,.0f} m³"
        ).add_to(m_res)
        
        st_folium(m_res, width=1200, height=500, key="result_map")
