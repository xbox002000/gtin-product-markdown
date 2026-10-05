"""Sample provider JSON fixtures (no real keys)."""

UPCITEMDB_OK = {
    "code": "OK",
    "total": 1,
    "offset": 0,
    "items": [
        {
            "ean": "0885909950805",
            "title": "Apple iPhone 6, Space Gray, 64 GB (T-Mobile)",
            "description": "Retina HD display smartphone.",
            "upc": "885909950805",
            "brand": "Apple",
            "model": "MG5A2LL/A",
            "color": "Space Gray",
            "size": "64GB",
            "category": "Electronics > Mobile Phones",
            "images": [
                "https://example.com/iphone.jpg",
            ],
            "asin": "B00NH11KIK",
        }
    ],
}

UPCITEMDB_EMPTY = {"code": "OK", "total": 0, "offset": 0, "items": []}

UPCITEMDB_DENIED = {"code": "INVALID_OR_MISSING_API_KEY", "message": "Invalid user_key"}

BARCODELOOKUP_OK = {
    "products": [
        {
            "barcode_number": "0885909950805",
            "title": "Apple iPhone 6 64GB",
            "brand": "Apple",
            "category": "Electronics > Communications > Telephony",
            "description": "Smartphone",
            "images": ["https://example.com/bl-iphone.jpg"],
            "asin": "B00NH11KIK",
            "size": "64GB",
            "mpn": "MG5A2LL/A",
        }
    ]
}

BARCODELOOKUP_EMPTY = {"products": []}

OFF_OK = {
    "status": 1,
    "code": "3017620422003",
    "product": {
        "code": "3017620422003",
        "product_name": "Nutella",
        "brands": "Nutella, Ferrero",
        "quantity": "400 g e",
        "categories": "Spreads, Sweet spreads, Cocoa and hazelnuts spreads",
        "image_front_url": "https://images.openfoodfacts.org/images/products/301/762/042/2003/front_en.400.jpg",
        "image_url": "https://images.openfoodfacts.org/images/products/301/762/042/2003/front_en.400.jpg",
        "ingredients_text": "Sugar, palm oil, hazelnuts 13%, cocoa 7.4%, skimmed milk powder 6.6%",
        "nutrition_grades": "e",
        "nutriments": {
            "energy-kcal_100g": 539,
            "fat_100g": 30.9,
            "saturated-fat_100g": 10.6,
            "carbohydrates_100g": 57.5,
            "sugars_100g": 56.3,
            "proteins_100g": 6.3,
            "salt_100g": 0.107,
        },
        "generic_name": "Hazelnut cocoa spread",
    },
}

OFF_MISS = {"status": 0, "status_verbose": "product not found", "code": "0000000000000"}
