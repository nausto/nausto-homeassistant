# Nausto for Home Assistant

Del valgte sensorer med båten din i [Nausto](https://nausto.no). Integrasjonen er levert av
Nausto og er en egen integrasjon, ikke en del av Home Assistant Core.

## Installere og koble til

Home Assistant **2026.10.4 eller nyere**. Prøvd med 2026.10.4 og 2026.11.0.
Integrasjonen kjører inne i HA og fungerer med HAOS og Container. Den trenger verken MQTT,
egen tjeneste eller en Home Assistant-tilgangsnøkkel hos Nausto.

1. Åpne **HACS → menyen med tre prikker → Egendefinerte kilder**. Legg til
   `https://github.com/nausto/nausto-homeassistant`, type **Integration**. Last ned **Nausto**
   og start Home Assistant på nytt når HACS ber om det. HACS må være installert først.
   På HA med Marketplace brukes samme repository som egendefinert integrasjonskilde der
   dette tilbys. Integrasjonen er ikke oppført i den innebygde katalogen.
2. I Nausto: åpne **Båten → Sensorer → Tilkoblinger og sensorer**, opprett en tilkobling
   for **Home Assistant**, og kopier paringskoden. Du må eie båten. Koden varer ti minutter.
3. I Home Assistant: **Innstillinger → Enheter og tjenester → Legg til integrasjon → Nausto**.
   Lim inn koden i feltet **Paringskode**, og velg bare entitetene som tilhører båten.
4. Tilbake i Nausto velger du **Følg denne sensoren**, hvilken komponent den tilhører og
   eventuell primærkilde. Målinger og historikk vises når delingen er aktivert begge steder.

Du kan også installere manuelt: kopier utgivelsens `custom_components/nausto` til HA-mappen
`config/custom_components/nausto`, start HA på nytt, og følg steg 2–4. Ingen YAML er nødvendig.
Koder og nøkler skal ikke legges i chat, logger eller feilrapporter.

## Valg, tilstander og tid

**Konfigurer** på integrasjonen lar deg endre det lokale delingsvalget. Et lokalt fjernet valg
stopper videre deling og fjerner det fra den usendte køen. En forseglet batch omskrives ikke;
inneholder den også en fjernet kanal, kasseres hele batchen og tapet telles i diagnostikken.
Nausto beholder allerede mottatt historikk. Deaktiver eller tilbakekall tilkoblingen i Nausto
for å stoppe mottak på serveren. En ny kode for en eksisterende tilkobling brukes via HAs
vanlige forespørsel om ny autentisering. En tilbakekalt tilkobling erstattes med en ny i Nausto
og legges til som ny integrasjon i HA. En kode for en annen tilkobling kan ikke flytte køen.

Bortvalg av en kanal i Nausto stanser mottak for kanalen. HA avstemmer også usendte opptak
ved neste kontroll av kanalvalgene, uten å endre forseglede batcher. Kassert innhold telles
som datagap. De gjenværende sensorene fortsetter, også hvis valget ble endret mellom et
livstegn og en sending.

Sensorer identifiseres med HAs entitetsregister og grupperes med enhetsregisteret. Navneendring
beholder identiteten; en erstattet entitet med ny register-ID krever et nytt delingsvalg.
Navn brukes ikke til å gjette hva som måles. Maksimalt 64 entiteter fra 16 enheter deles per
tilkobling, med opptil fem lokale tilkoblingsprofiler.

Støttede størrelser er spenning, strøm, effekt, brukt kapasitet, temperatur, prosent, hastighet,
varighet, binære tilstander/alarmer og enum-tilstander med dokumenterte enheter. Batteriprosent
merkes estimert, øvrige HA-verdier avledet: HA kan selv ha beregnet dem. Energi, trykk og volum
er ikke del av denne kontrakten. Ukjent og utilgjengelig sendes som egne kvaliteter uten en
oppdiktet 0 eller av-verdi. Naustos komponentvalg og primærkilde er alltid brukerens valg.

HA `last_updated` er tidspunktet da tilstanden eller attributtene sist ble endret, ikke bevis for
når den fysiske målingen ble tatt. `last_reported` brukes som en separat kildebekreftelse når
HA faktisk har skrevet tilstanden, også uendret. Periodisk lesing og nettverkslivstegn endrer
ingen av disse tidene. Gjenopprettede tilstander deles ikke før en ny rapport. En integration
som selv republiserer sin cache, beviser ikke ny fysisk måling; vurder kildekvaliteten i HA.

## Kø og feilsøking

Opptak lagres privat i `config/.storage/nausto/`, og forsegles omtrent hvert minutt.
Numeriske verdier begrenses til ett opptak per kanal per minutt; tilstands- og
tilgjengelighetsendringer bevares raskere. Samme forseglete bytes og måle-ID-er brukes ved
gjentatt sending og omstart. Sending begrenses til én forespørsel per 30 sekunder, med
eksponentiell venting og respekt for serverens `Retry-After` (maksimalt én time).

Køen rommer høyst 10 000 observasjoner, 10 MiB og sju dager per profil inkludert åpent opptak.
Gamle eller overskytende batcher kasseres hele og telles som datagap. Etter tolv mislykkede
forsøk beholdes opptaket som avvist. **Konfigurer → Prøv tilkoblingen på nytt** åpner for
nye forsøk. Home Assistant viser et reparasjonsvarsel ved permanente avvisninger eller tap.
Last ned integrasjonens **diagnostikk** for køtellere, gap og feiltype. Diagnostikken har ingen
paringskode, produsentnøkkel, entitetsnavn, verdier eller private nettadresser.

Hvis HA er avslått, integrasjonen er deaktivert eller hendelseskøen er full, kan målinger gå
tapt. Historikk rekonstrueres ikke fra HAs Recorder. Ved oppstart brukes bare en faktisk
rapport som fortsatt finnes i tilstandsmaskinen; tidsrommet uten opptak er et hull.
En full HA-sikkerhetskopi kan inneholde den private produsentnøkkelen og køen: beskytt den.
En gjenoppretting med endret HA-instans-ID krever et nytt oppsett og gjenbruker ikke gammel kø.
Fjern de gamle Nausto-integrasjonene i HA og legg til på nytt. Når siste integrasjon fjernes,
slettes de lokale profilene og nøklene; allerede mottatt historikk i Nausto beholdes.

Utgående trafikk går bare til Naustos HTTPS-mottak. Tilkoblingsnøkkelen er bundet til én båt;
den gir ikke rett til å lese Nausto-data eller styre utstyr. Ingen kommandoer sendes til reléer,
ladere, foreldrekontroll eller andre HA-enheter.

## English

Add `https://github.com/nausto/nausto-homeassistant` as a custom **Integration** repository in
HACS, download **Nausto**, and restart Home Assistant. In Nausto, open your boat's sensors,
create a Home Assistant connection and copy the ten-minute pairing code. In HA, add the
**Nausto** integration, paste the code and select the boat entities to share. Return to Nausto
to follow each channel, assign its component and choose its primary source. The setup and
options forms are available in English and Norwegian.

Only explicitly selected entities are offered. HA registry IDs keep identities stable through
renames. Source state-change time and source report confirmation remain separate; a gateway
heartbeat never refreshes a measurement. Unknown/unavailable are preserved. A private SQLite
queue survives restarts and retries identical batches. Its seven-day / 10,000-observation /
10-MiB limits and missing HA runtime can leave gaps. Diagnostics expose counts and error types,
never credentials or sensor data. Use **Configure** to change sharing or retry rejected data.
Unselecting a channel in Nausto also reconciles queued captures at the next selection check.
Sealed batches remain immutable; discarded captures count as gaps, while other sensors continue.

Copyright Nausto. Alle rettigheter forbeholdt. Offentlig kildekode gir ikke i seg selv en
lisens til annen bruk eller til Naustos merkevare.
