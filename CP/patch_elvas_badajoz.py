import pypdf

def remplir_pdf_dynamique(pdf_entree, pdf_sortie, donnees_utilisateur):
    """
    Inspecte dynamiquement les champs d'un formulaire PDF et les remplit.
    S'adapte automatiquement si le formulaire évolue.
    """
    # 1. Chargement du document PDF
    reader = pypdf.PdfReader(pdf_entree)
    
    # 2. Inspection dynamique des champs interactifs existants
    champs_detectes = reader.get_fields() or {}
    
    print(f"--- Champs détectés dans le PDF ({len(champs_detectes)}) ---")
    valeurs_a_mettre_a_jour = {}
    
    for nom_champ, infos in champs_detectes.items():
        type_champ = infos.get('/FT', 'Inconnu')
        print(f"Champ : {nom_champ} | Type : {type_champ}")
        
        # Si la donnée utilisateur existe pour ce champ, on la prépare
        if nom_champ in donnees_utilisateur:
            valeurs_a_mettre_a_jour[nom_champ] = donnees_utilisateur[nom_champ]

    # 3. Préparation de l'écriture en préservant la structure AcroForm
    writer = pypdf.PdfWriter(clone_from=reader)

    # Forcer la régénération visuelle des champs modifiés
    writer.set_need_appearances_writer(True)

    # Application des valeurs mises à jour sur toutes les pages
    for page in writer.pages:
        writer.update_page_form_field_values(page, valeurs_a_mettre_a_jour)

    # 4. Enregistrement du PDF final
    with open(pdf_sortie, "wb") as f:
        writer.write(f)

    print(f"\nDocument généré avec succès : {pdf_sortie}")


# --- EXEMPLE D'UTILISATION ---
donnees = {
    "Nom": "Dupont",
    "Prenom": "Jean",
    "Email": "jean.dupont@example.com",
    "Date": "2026-09-20"
}

# Assurez-vous que pypdf est installé (pip install pypdf)
remplir_pdf_dynamique("formulaire_entree.pdf", "formulaire_rempli.pdf", donnees)