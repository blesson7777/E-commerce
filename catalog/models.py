from django.core.validators import FileExtensionValidator
from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Product(models.Model):
    seller = models.ForeignKey(
        'accounts.User',
        on_delete=models.CASCADE,
        related_name='products',
        limit_choices_to={'role': 'seller'},
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    location = models.ForeignKey(
        'locations.Location',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products',
    )
    serviceable_states = models.ManyToManyField(
        'locations.State',
        blank=True,
        related_name='products_serviceable_by_state',
    )
    serviceable_districts = models.ManyToManyField(
        'locations.District',
        blank=True,
        related_name='products_serviceable_by_district',
    )
    serviceable_locations = models.ManyToManyField(
        'locations.Location',
        blank=True,
        related_name='products_serviceable_by_location',
    )
    non_serviceable_locations = models.ManyToManyField(
        'locations.Location',
        blank=True,
        related_name='products_non_serviceable_by_location',
    )
    name = models.CharField(max_length=150)
    description = models.TextField()
    photo = models.FileField(
        upload_to='product_photos/',
        blank=True,
        null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=['jpg', 'jpeg', 'png', 'webp']),
        ],
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock_quantity = models.PositiveIntegerField(default=0)
    weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    size = models.CharField(max_length=60, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('seller', 'name')

    def __str__(self):
        return self.name

    @staticmethod
    def is_location_active(location):
        if not location or not location.district_id:
            return False
        district = location.district
        state = district.state if district and district.state_id else None
        return bool(
            location.is_active
            and district.is_active
            and (state.is_active if state else True)
        )

    def _prefetched_relation_ids(self, relation_name):
        cache = getattr(self, '_prefetched_objects_cache', {})
        if relation_name in cache:
            return {obj.id for obj in cache[relation_name]}
        return None

    def _prefetched_relation_objects(self, relation_name):
        return getattr(self, '_prefetched_objects_cache', {}).get(relation_name)

    def _relation_contains(self, relation_name, object_id):
        if object_id is None:
            return False
        prefetched_ids = self._prefetched_relation_ids(relation_name)
        if prefetched_ids is not None:
            return object_id in prefetched_ids
        return getattr(self, relation_name).filter(id=object_id).exists()

    def has_service_area_configured(self):
        for relation_name in ('serviceable_states', 'serviceable_districts', 'serviceable_locations'):
            prefetched_ids = self._prefetched_relation_ids(relation_name)
            if prefetched_ids is not None:
                if prefetched_ids:
                    return True
            elif getattr(self, relation_name).exists():
                return True
        return False

    def is_serviceable_for_location(self, location):
        if not self.is_location_active(location):
            return False

        if self._relation_contains('non_serviceable_locations', location.id):
            return False

        district = location.district if location.district_id else None
        state = district.state if district and district.state_id else None
        district_id = district.id if district else None
        state_id = state.id if state else None

        if self._relation_contains('serviceable_locations', location.id):
            return True

        if self._relation_contains('serviceable_districts', district_id):
            return True

        if self._relation_contains('serviceable_states', state_id):
            state_ids_with_district_rules = getattr(
                self,
                '_serviceable_district_state_ids_cache',
                None,
            )
            if state_ids_with_district_rules is None:
                prefetched_districts = self._prefetched_relation_objects('serviceable_districts')
                if prefetched_districts is not None:
                    state_ids_with_district_rules = {
                        district.state_id
                        for district in prefetched_districts
                        if district.state_id
                    }
                else:
                    state_ids_with_district_rules = set(
                        self.serviceable_districts.values_list('state_id', flat=True)
                    )
                self._serviceable_district_state_ids_cache = state_ids_with_district_rules

            # If a state has explicit district selections, those districts are the allowed scope.
            if state_id in state_ids_with_district_rules:
                return False
            return True

        if not self.has_service_area_configured() and self.location_id:
            return self.location_id == location.id
        return False

# Create your models here.
