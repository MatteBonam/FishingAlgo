import requests
import json
import streamlit as st
import pandas as pd
import altair as alt
from scipy.interpolate import make_interp_spline
from datetime import datetime
import folium
from streamlit_folium import st_folium
import openmeteo_requests
import requests_cache
from retry_requests import retry

def calcola_data(data):
    st.write(data)
    # Estrai solo la data
    date_only = pd.to_datetime(data)
    return date_only

def calcola_fase_lunare(data):
    st.write(data)
    # Data di riferimento per la Luna Nuova (ad esempio il 6 gennaio 2000)
    riferimento = pd.datetime(2000, 1, 6)
    st.write(riferimento)
    # Calcolo dei giorni passati dalla data di riferimento
    giorni_passati = (data - riferimento).days
    # La durata di un ciclo lunare medio è di 29,53 giorni
    ciclo_lunare = 29.53
    # Fase attuale (0 = luna nuova, 14-15 = luna piena)
    fase_attuale = (giorni_passati % ciclo_lunare) / ciclo_lunare
    
    # Restituisce la fase lunare come percentuale (0.0 = luna nuova, 0.5 = luna piena)
    return fase_attuale

def calcola_stagione(data):
    # Mese attuale
    mese_attuale = data.month

    # Definizione delle stagioni (in base a climi temperati italiani)
    if mese_attuale in [12, 1, 2]:
        return "inverno"
    elif mese_attuale in [3, 4, 5]:
        return "primavera"
    elif mese_attuale in [6, 7, 8]:
        return "estate"
    elif mese_attuale in [9, 10, 11]:
        return "autunno"

def calcola_attivita_pesca(specie, dati_giorno):
    temperatura = dati_giorno['temperature']
    pressione = dati_giorno['pressure']
    vento_velocita = dati_giorno['wind']
    nuvolosita = dati_giorno['clouds']
    pioggia = dati_giorno['rain']
    data_corrente = calcola_data(dati_giorno['time'])

        # Punteggio di attività iniziale
    attivita = 100  # partiamo da 100 e lo riduciamo proporzionalmente alle condizioni
    # Fase lunare (supponendo che la funzione calcola_fase_lunare() sia già definita)
    fase_lunare = calcola_fase_lunare(data_corrente)
    # Fase lunare ideale: attorno a luna piena (0.4 <= fase_lunare <= 0.6)
    attivita *= (1 - abs(fase_lunare - 0.5) * 2)  # riduzione graduale se lontana dalla fase ottimale
    # Calcolo della stagione
    stagione = calcola_stagione(data_corrente)
    # Condizioni ideali di temperatura
    if specie == "persico trota":
        temp_ideale_min, temp_ideale_max = 18, 26
    elif specie == "luccio":
        temp_ideale_min, temp_ideale_max = 10, 22
    if temperatura < temp_ideale_min:
        scarto_temp = temp_ideale_min - temperatura
        attivita *= max(0, (1 - (scarto_temp / 10) ** 2))  # scalare quadratico: l'influenza aumenta allontanandosi dal range
    elif temperatura > temp_ideale_max:
        scarto_temp = temperatura - temp_ideale_max
        attivita *= max(0, (1 - (scarto_temp / 10) ** 2))
    else:
        # Anche nel range ideale c'è una lieve riduzione a seconda di quanto vicina sia ai limiti
        centro_ideale_temp = (temp_ideale_min + temp_ideale_max) / 2
        scarto_temp = abs(temperatura - centro_ideale_temp) / ((temp_ideale_max - temp_ideale_min) / 2)
        attivita *= max(0, (1 - (scarto_temp ** 2) / 4))  # penalità leggera dentro il range ideale
    # Influenza della stagione sull'attività
    stagione_attivita = {
        "luccio": {"primavera": 1.15, "estate": 0.9, "autunno": 1.2, "inverno": 0.8},
        "persico trota": {"primavera": 1.2, "estate": 0.9, "autunno": 1.15, "inverno" : 0.7},
    }
    # Applicare la variazione stagionale all'attività
    if specie in stagione_attivita:
        attivita *= stagione_attivita[specie].get(stagione, 0)
    # Influenza della pressione atmosferica
    if specie == "persico trota":
        pressione_ideale = 1020
    elif specie == "luccio":
        pressione_ideale = 1015
    # Riduzione proporzionale in base alla distanza dalla pressione ideale
    scarto_pressione = abs(pressione - pressione_ideale) / pressione_ideale
    if scarto_pressione <= 1:
        attivita *= (1 - (scarto_pressione ** 2) / 4)  # penalità leggera quando si è vicini alla pressione ideale
    else:    
        attivita *= max(0, (1 - (scarto_pressione ** 2)))  
    # Influenza della nuvolosità
    nuvolosita_ideale = 50  # attività massima attorno al 50% di nuvolosità
    scarto_nuvolosita = abs(nuvolosita - nuvolosita_ideale) / 50  # scalare basato su quanto ci si allontana da 50%
    if scarto_nuvolosita <= 1:
        # Anche nel range ideale c'è una lieve riduzione a seconda di quanto vicina sia al centro ideale
        attivita *= (1 - (scarto_nuvolosita ** 2) / 4)  # penalità leggera quando si è vicini al 50%
    else:
        attivita *= max(0, (1 - (scarto_nuvolosita ** 2)))
    # Influenza della pioggia
    if specie == 'persico trota':
        if pioggia <= 2:
            attivita *= 1.2  # pioggia leggera aumenta leggermente l'attività
        elif pioggia <= 10:
            attivita *= 1.0  
        else:
            attivita *= 0.7  # temporali riducono molto l'attività
    elif specie == 'luccio':
        if pioggia <= 2:
            attivita *= 1.3  # pioggia leggera aumenta leggermente l'attività
        elif pioggia <= 10:
            attivita *= 1.1  
        else: 
            attivita *= 0.6  # temporali riducono molto l'attività
    # Influenza del vento
    if vento_velocita > 15:
        attivita *= 0.5  # forte vento riduce drasticamente l'attività
    elif vento_velocita > 5:
        attivita *= 0.9  # vento moderato riduce leggermente l'attività
    
    return attivita

st.title("Previsioni Meteo")

latitude = 44.59  # Coordinata di Bologna
longitude = 11.34

# Setup the Open-Meteo API client with cache and retry on error
cache_session = requests_cache.CachedSession('.cache', expire_after = 3600)
retry_session = retry(cache_session, retries = 5, backoff_factor = 0.2)
openmeteo = openmeteo_requests.Client(session = retry_session)
                                      
# Se non c'è uno stato iniziale del marker, lo inizializziamo
if 'marker' not in st.session_state:
    st.session_state.marker = None

if st.session_state.marker:
    latitude, longitude= st.session_state.marker['lat'], st.session_state.marker['lng']

url = f"https://api.open-meteo.com/v1/forecast"
params = {
    "latitude": latitude,
    "longitude" : longitude,
    "hourly" : ["temperature_2m","wind_speed_10m","surface_pressure","cloud_cover","rain"],
    "forecast_days" : 14
}
response = openmeteo.weather_api(url, params)
response = response[0]
hourly = response.Hourly()
hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
hourly_wind = hourly.Variables(1).ValuesAsNumpy()
hourly_pressure = hourly.Variables(2).ValuesAsNumpy()
hourly_clouds = hourly.Variables(3).ValuesAsNumpy()
hourly_rain = hourly.Variables(4).ValuesAsNumpy()

hourly_data = {"time": pd.date_range(
	start = pd.to_datetime(hourly.Time(), unit = "s", utc = True),
	end = pd.to_datetime(hourly.TimeEnd(), unit = "s", utc = True),
	freq = pd.Timedelta(seconds = hourly.Interval()),
	inclusive = "left"
)}

hourly_data["temperature"] = hourly_temperature_2m
hourly_data["wind"] = hourly_wind
hourly_data["pressure"] = hourly_pressure
hourly_data["clouds"] = hourly_clouds
hourly_data["rain"] = hourly_rain
hourly_dataframe = pd.DataFrame(data = hourly_data)

specie = st.radio(
    "Di quale pesce vuoi conoscere l'attivita?",
    ["luccio", "persico trota"],
    captions=[
        "Pyke.",
        "Black Bass.",
    ],
)

hourly_dataframe['time'] = pd.to_datetime(hourly_dataframe['time'])
# Aggiungi anche i dati dalla sezione 'main'
hourly_dataframe['latitude'] = latitude
hourly_dataframe['longitude'] = longitude
#df['attivita'] = calcola_attivita_pesca(specie, df)
# Show a slider widget with the years using `st.slider`.
hour = st.slider("Fascia oraria scelta", 0, 24, (12, 15))
for index, row in hourly_dataframe.iterrows() :
    item = {
        "time": row["time"],
        "temperature": row["temperature"],
        "rain": row["rain"],
        "pressure": row["pressure"],
        "clouds": row["clouds"],
        "wind": row["wind"]
    }
    st.write(item)
    attivita = calcola_attivita_pesca(specie, item)
    hourly_dataframe.at[index, 'attivita'] = (attivita)
# Filter the dataframe based on the widget input and reshape it.
df_filtered = hourly_dataframe[(hourly_dataframe['time'].between(hour[0], hour[1]))]

chart = alt.Chart(df_filtered).mark_line().encode(
    x='hour:O',  # O indica "ordinal" per l'asse delle ore
    y='attivita:Q',  # Q indica "quantitative" per l'asse delle temperature
    color='date:N'  # N indica "nominal" per le date
).properties(
    title='Attività previste per i prossimi 7 giorni'
)

# Display the data as a table using `st.dataframe`.
if st.checkbox('Show raw data', value=True):
    st.subheader('Raw data')
    st.write(df_filtered)


inizio_mappa = [latitude, longitude]


# Creare una mappa di base
m = folium.Map(location=inizio_mappa, zoom_start=12)

# Se esiste un marker, lo aggiungiamo alla mappa
if st.session_state.marker:
    lat, lng = st.session_state.marker['lat'], st.session_state.marker['lng']
    folium.Marker([lat, lng], popup="Punto selezionato").add_to(m)

# Visualizzare la mappa
output = st_folium(m, width=725, height=500, key="mappa_interattiva")

# Se è stato fatto clic sulla mappa
if output and output['last_clicked'] is not None:
    lat = output['last_clicked']['lat']
    lng = output['last_clicked']['lng']
    
    # Salvare il marker nel session state
    st.session_state.marker = {'lat': lat, 'lng': lng}
    
    # Aggiungere il marker alla mappa
    m = folium.Map(location=inizio_mappa, zoom_start=12)
    folium.Marker([lat, lng], popup="Punto selezionato").add_to(m)
    
    # Aggiornare la visualizzazione della mappa con il marker
    st_folium(m, width=725, height=500, key="mappa_interattiva")


st.altair_chart(chart, use_container_width=True)