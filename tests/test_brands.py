"""Own-brand detection tests, using real product names from the catalogues."""

import pytest

from tickets import brands


@pytest.mark.parametrize(
    "chain, name, expected",
    [
        # MAS: the shop's own brand pages and the catalogue agree.
        ("MAS", "Albondigas IFA ELIGES lata 415gr", "IFA ELIGES"),
        ("MAS", "Maxi tortilla de trigo IFA ELIGES 6ud 370gr", "IFA ELIGES"),
        ("MAS", "Refresco cola IFA ELIGES lata 33cl", "IFA ELIGES"),
        ("MAS", "Compresa clasica unnia 20ud", "Unnia"),
        ("MAS", "Bebida soja sin azucar IFA ELIGES 1l", "IFA ELIGES"),
        # Dia: dia_brand facet plus the Vegecampo line.
        ("Dia", "Corazones de alcachofas Dia Vegecampo 115 g", "Vegecampo"),
        ("Dia", "Espinacas Dia Vegecampo 250 g", "Vegecampo"),
        ("Dia", "Leche entera Dia 1 l", "Dia"),
        # Mercadona: no brand attribute, consistent own labels in the name.
        ("Mercadona", "Aceite de oliva 0,4º Hacendado", "Hacendado"),
        ("Mercadona", "Lejía Bosque Verde 2 l", "Bosque Verde"),
        ("Mercadona", "Crema hidratante Deliplus", "Deliplus"),
    ],
)
def test_detects_the_own_brand_of_real_products(chain, name, expected):
    assert brands.detect_own_brand(chain, name) == expected


@pytest.mark.parametrize(
    "chain, name",
    [
        ("MAS", "Champiñon blanco bandeja 400gr"),
        ("MAS", "Bacalao filetes PESCANOVA"),
        ("Dia", "Naranja mesa"),
        ("Dia", "Merluza empanada"),
        ("Mercadona", "Aceite de girasol refinado 0,2º"),
        ("Mercadona", "Plátano de Canarias"),
    ],
)
def test_national_brands_are_not_own_brands(chain, name):
    assert brands.detect_own_brand(chain, name) is None
    assert brands.is_own_brand(chain, name) is False


@pytest.mark.parametrize(
    "chain, name",
    [
        # Substring matching would wrongly flag these.
        ("Dia", "Diadema de flores"),
        ("Dia", "Medialuna de chocolate"),
        ("MAS", "Ifanescas del sur"),
        ("MAS", "Unniamiento de motor"),
        ("Mercadona", "Compyuter de sobremesa"),
    ],
)
def test_whole_token_matching_avoids_lookalikes(chain, name):
    assert brands.detect_own_brand(chain, name) is None


def test_multiword_brand_wins_over_its_first_word():
    assert brands.detect_own_brand("MAS", "Fabada asturiana IFA ELIGES lata 430gr") == "IFA ELIGES"


def test_accents_and_case_do_not_matter():
    assert brands.detect_own_brand("Dia", "ESPINACAS dia VEGECAMPO 250 G") == "Vegecampo"
    assert brands.detect_own_brand("Mercadona", "lejía bosque verde") == "Bosque Verde"


def test_unknown_chain_has_no_brands():
    assert brands.own_brands("Lidl") == ()
    assert brands.detect_own_brand("Lidl", "Leche Hacendado") is None


def test_empty_input_is_not_a_brand():
    assert brands.detect_own_brand("Dia", None) is None
    assert brands.detect_own_brand("Dia", "") is None
    assert brands.brand_tokens("") == []


def test_brand_tokens_are_accent_free_uppercase():
    assert brands.brand_tokens("Crema hidratante Deliplus 250 ml") == [
        "CREMA",
        "HIDRATANTE",
        "DELIPLUS",
        "250",
        "ML",
    ]
