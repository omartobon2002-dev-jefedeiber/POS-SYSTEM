"""Tax becomes a per-business setting, and the rest of the business
configuration moves here from the browser's localStorage.

`default_tax_rate` is *renamed* rather than dropped and recreated: existing
tenants keep the rate they had, and `charges_tax` defaults to True so their
sales keep behaving exactly as before.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='organization',
            old_name='default_tax_rate',
            new_name='tax_rate',
        ),
        migrations.AlterField(
            model_name='organization',
            name='tax_rate',
            field=models.DecimalField(
                decimal_places=2,
                default=19,
                help_text='Percentage. Ignored while charges_tax is False.',
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.RemoveField(
            model_name='organization',
            name='prices_include_tax',
        ),
        migrations.AddField(
            model_name='organization',
            name='charges_tax',
            field=models.BooleanField(default=True, help_text='Does this business sell with IVA?'),
        ),
        migrations.AddField(
            model_name='organization',
            name='tax_regime',
            field=models.CharField(
                choices=[
                    ('RESPONSABLE_IVA', 'Responsable de IVA'),
                    ('NO_RESPONSABLE_IVA', 'No responsable de IVA'),
                    ('REGIMEN_SIMPLE', 'Régimen Simple de Tributación'),
                ],
                default='RESPONSABLE_IVA',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='organization',
            name='phone',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name='organization',
            name='address',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='organization',
            name='city',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='organization',
            name='dian_resolution',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='organization',
            name='dian_resolution_range',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='organization',
            name='dian_resolution_valid_until',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='organization',
            name='receipt_footer',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='organization',
            name='receipt_paper_width',
            field=models.CharField(
                choices=[('80mm', '80 mm'), ('58mm', '58 mm')], default='80mm', max_length=4
            ),
        ),
    ]
