"""Products and variants: a SKU/barcode is unique across the business, not per product."""
from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/products/"


def _image_file(name="foto.jpg", fmt="JPEG", content_type="image/jpeg", size=(20, 20)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color="red").save(buffer, format=fmt)
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type=content_type)


def _body(name, variants):
    return {"name": name, "variants": variants}


def test_two_products_cannot_reuse_the_same_sku(tenant_a, client_for):
    """The exact collision reported in production: two products, one generated SKU."""
    client = client_for(tenant_a.owner)
    first = client.post(
        PRODUCTS,
        _body("Zapatillas nike", [{"sku": "ZAP-ZAPA-AZU-43", "price": "50000"}]),
        format="json",
    )
    assert first.status_code == 201

    second = client.post(
        PRODUCTS,
        _body("Zapatos adidas", [{"sku": "ZAP-ZAPA-AZU-43", "price": "50000"}]),
        format="json",
    )

    # A readable 400, never a raw IntegrityError/500.
    assert second.status_code == 400
    assert "ZAP-ZAPA-AZU-43" in str(second.data["variants"])


def test_two_products_cannot_reuse_the_same_barcode(tenant_a, client_for):
    client = client_for(tenant_a.owner)
    client.post(
        PRODUCTS,
        _body("Camiseta", [{"sku": "CAM-1", "barcode": "7701234567890", "price": "30000"}]),
        format="json",
    )

    response = client.post(
        PRODUCTS,
        _body("Otra camiseta", [{"sku": "CAM-2", "barcode": "7701234567890", "price": "30000"}]),
        format="json",
    )

    assert response.status_code == 400
    assert "7701234567890" in str(response.data["variants"])


def test_duplicate_sku_within_the_same_request_is_still_rejected(tenant_a, client_for):
    response = client_for(tenant_a.owner).post(
        PRODUCTS,
        _body(
            "Producto",
            [
                {"sku": "DUP-1", "price": "10000"},
                {"sku": "DUP-1", "price": "10000"},
            ],
        ),
        format="json",
    )

    assert response.status_code == 400


def test_a_product_keeps_its_own_sku_when_edited(tenant_a, client_for):
    """Updating a product must not flag its own variant's SKU as taken."""
    client = client_for(tenant_a.owner)
    created = client.post(PRODUCTS, _body("Buso", [{"sku": "BUS-1", "price": "80000"}]), format="json").data
    variant_id = created["variants"][0]["id"]

    response = client.patch(
        f"{PRODUCTS}{created['id']}/",
        {"variants": [{"id": variant_id, "sku": "BUS-1", "price": "85000"}]},
        format="json",
    )

    assert response.status_code == 200


def test_a_sku_taken_by_another_product_cannot_be_reused_when_editing(tenant_a, client_for):
    client = client_for(tenant_a.owner)
    client.post(PRODUCTS, _body("Producto A", [{"sku": "A-1", "price": "1000"}]), format="json")
    other = client.post(PRODUCTS, _body("Producto B", [{"sku": "B-1", "price": "1000"}]), format="json").data

    response = client.patch(
        f"{PRODUCTS}{other['id']}/",
        {"variants": [{"id": other["variants"][0]["id"], "sku": "A-1", "price": "1000"}]},
        format="json",
    )

    assert response.status_code == 400


def test_the_sku_conflict_is_scoped_to_the_business(tenant_a, tenant_b, client_for):
    """The same SKU is free in an unrelated business."""
    client_for(tenant_a.owner).post(
        PRODUCTS, _body("Producto", [{"sku": "SAME-SKU", "price": "1000"}]), format="json"
    )

    response = client_for(tenant_b.owner).post(
        PRODUCTS, _body("Producto", [{"sku": "SAME-SKU", "price": "1000"}]), format="json"
    )

    assert response.status_code == 201


def test_a_product_can_be_created_without_a_brand(tenant_a, client_for):
    response = client_for(tenant_a.owner).post(
        PRODUCTS, {"name": "Camiseta básica", "variants": []}, format="json"
    )

    assert response.status_code == 201, response.data
    assert response.data["brand"] is None


# -- Photo -----------------------------------------------------------------


@pytest.fixture
def product(tenant_a, client_for):
    return client_for(tenant_a.owner).post(PRODUCTS, _body("Camiseta con foto", []), format="json").data


def test_uploading_a_photo_sets_it_on_the_product(tenant_a, product, client_for):
    client = client_for(tenant_a.owner)

    response = client.post(f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart")

    assert response.status_code == 200, response.data
    assert response.data["image"].endswith(".jpg")
    assert response.data["image"].startswith("http")


def test_a_product_has_no_photo_until_one_is_uploaded(product):
    assert product["image"] is None


def test_reuploading_replaces_the_previous_photo_file(tenant_a, product, client_for):
    """The old file must not linger on disk once it is no longer referenced."""
    client = client_for(tenant_a.owner)
    first = client.post(
        f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart"
    ).data
    first_path = first["image"].split("/media/")[-1]

    second = client.post(
        f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart"
    ).data

    from django.core.files.storage import default_storage

    assert second["image"] != first["image"]
    assert not default_storage.exists(f"products/{tenant_a.org.pk}/{first_path.split('/')[-1]}")


def test_deleting_the_photo_clears_it(tenant_a, product, client_for):
    client = client_for(tenant_a.owner)
    client.post(f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart")

    response = client.delete(f"{PRODUCTS}{product['id']}/photo/")

    assert response.status_code == 200
    assert response.data["image"] is None


def test_a_non_image_file_is_rejected(tenant_a, product, client_for):
    client = client_for(tenant_a.owner)
    fake = SimpleUploadedFile("notas.txt", b"esto no es una imagen", content_type="text/plain")

    response = client.post(f"{PRODUCTS}{product['id']}/photo/", {"image": fake}, format="multipart")

    assert response.status_code == 400


def test_an_unsupported_image_format_is_rejected(tenant_a, product, client_for):
    """BMP decodes fine in Pillow but is not on the allowed list."""
    client = client_for(tenant_a.owner)
    bmp = _image_file(name="foto.bmp", fmt="BMP", content_type="image/bmp")

    response = client.post(f"{PRODUCTS}{product['id']}/photo/", {"image": bmp}, format="multipart")

    assert response.status_code == 400
    assert "image" in response.data


def test_an_oversized_photo_is_rejected():
    """Unit-level: faking `.size` avoids actually shipping megabytes in the suite."""
    from apps.catalog.serializers import MAX_PHOTO_SIZE, ProductPhotoSerializer

    upload = _image_file()
    upload.size = MAX_PHOTO_SIZE + 1

    serializer = ProductPhotoSerializer(data={"image": upload})

    assert not serializer.is_valid()
    assert "image" in serializer.errors


def test_a_cashier_can_upload_a_product_photo(tenant_a, product, make_employee, client_for):
    from apps.accounts.models import Membership

    cashier = make_employee(tenant_a, role=Membership.Role.CASHIER)

    response = client_for(cashier).post(
        f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart"
    )

    assert response.status_code == 200
    assert response.data["image"]


def test_product_photos_never_cross_tenants(tenant_a, tenant_b, product, client_for):
    client_for(tenant_a.owner).post(
        f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart"
    )

    response = client_for(tenant_b.owner).post(
        f"{PRODUCTS}{product['id']}/photo/", {"image": _image_file()}, format="multipart"
    )

    assert response.status_code == 404
