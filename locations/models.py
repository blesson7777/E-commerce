from django.db import models


class State(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class District(models.Model):
    state = models.ForeignKey(
        State,
        on_delete=models.CASCADE,
        related_name='districts',
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['state__name', 'name']
        constraints = [
            models.UniqueConstraint(fields=['state', 'name'], name='locations_unique_state_district')
        ]

    def __str__(self):
        if self.state:
            return f'{self.name}, {self.state.name}'
        return self.name


class Location(models.Model):
    district = models.ForeignKey(District, on_delete=models.CASCADE, related_name='locations')
    name = models.CharField(max_length=120)
    postal_code = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['district__name', 'name']
        unique_together = ('district', 'name', 'postal_code')

    def __str__(self):
        return f'{self.name}, {self.district.name}'

# Create your models here.
