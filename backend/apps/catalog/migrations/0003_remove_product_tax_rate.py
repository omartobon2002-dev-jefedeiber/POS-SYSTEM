"""Tax is no longer a product attribute: it is configured once per business
(`Organization.charges_tax` / `tax_rate`). Sales already snapshot their own
rate on every SaleItem, so historical breakdowns are unaffected.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0002_product_image'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='product',
            name='tax_rate',
        ),
    ]
