from django.core.paginator import Paginator
from django.db.models import Q
from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render

from accounts.decorators import role_required
from accounts.models import User
from locations.forms import DistrictForm
from locations.forms import LocationForm
from locations.forms import StateForm
from locations.models import District
from locations.models import Location
from locations.models import State


@role_required(User.UserRole.ADMIN)
def state_list_create(request):
    if request.method == 'POST':
        form = StateForm(request.POST)
        if form.is_valid():
            state = form.save()
            if not state.is_active:
                District.objects.filter(state=state, is_active=True).update(is_active=False)
                Location.objects.filter(district__state=state, is_active=True).update(is_active=False)
            return redirect('locations:state_list')
    else:
        form = StateForm()

    query = request.GET.get('q', '').strip()
    states_queryset = State.objects.all()
    if query:
        states_queryset = states_queryset.filter(Q(name__icontains=query) | Q(code__icontains=query))

    context = {
        'form': form,
        'query': query,
        'states': Paginator(states_queryset, 50).get_page(request.GET.get('page')),
    }
    return render(request, 'locations/state_list.html', context)


@role_required(User.UserRole.ADMIN)
def state_edit(request, state_id):
    state = get_object_or_404(State, id=state_id)
    if request.method == 'POST':
        form = StateForm(request.POST, instance=state)
        if form.is_valid():
            state = form.save()
            if not state.is_active:
                District.objects.filter(state=state, is_active=True).update(is_active=False)
                Location.objects.filter(district__state=state, is_active=True).update(is_active=False)
                messages.info(
                    request,
                    f'All districts and locations under {state.name} were marked Not Available.',
                )
            else:
                district_count = District.objects.filter(state=state, is_active=False).update(is_active=True)
                location_count = Location.objects.filter(district__state=state, is_active=False).update(is_active=True)
                if district_count or location_count:
                    messages.success(
                        request,
                        f'{district_count} districts and {location_count} locations in {state.name} were restored to Available.',
                    )
            return redirect('locations:state_list')
    else:
        form = StateForm(instance=state)
    return render(request, 'locations/state_edit.html', {'form': form, 'state': state})


@role_required(User.UserRole.ADMIN)
def state_toggle_availability(request, state_id):
    state = get_object_or_404(State, id=state_id)
    if request.method == 'POST':
        state.is_active = request.POST.get('is_active') == 'on'
        state.save(update_fields=['is_active'])
        if not state.is_active:
            district_count = District.objects.filter(state=state, is_active=True).update(is_active=False)
            location_count = Location.objects.filter(district__state=state, is_active=True).update(is_active=False)
            messages.info(
                request,
                f'{district_count} districts and {location_count} locations in {state.name} were set to Not Available.',
            )
        else:
            district_count = District.objects.filter(state=state, is_active=False).update(is_active=True)
            location_count = Location.objects.filter(district__state=state, is_active=False).update(is_active=True)
            if district_count or location_count:
                messages.success(
                    request,
                    f'{district_count} districts and {location_count} locations in {state.name} were restored to Available.',
                )
    return redirect(request.META.get('HTTP_REFERER', 'locations:state_list'))


@role_required(User.UserRole.ADMIN)
def district_list_create(request):
    if request.method == 'POST':
        form = DistrictForm(request.POST)
        if form.is_valid():
            district = form.save()
            if not district.is_active:
                Location.objects.filter(district=district, is_active=True).update(is_active=False)
            return redirect('locations:district_list')
    else:
        form = DistrictForm()

    query = request.GET.get('q', '').strip()
    state_id = (request.GET.get('state') or '').strip()
    district_queryset = District.objects.select_related('state').all()
    if state_id:
        district_queryset = district_queryset.filter(state_id=state_id)
    if query:
        district_queryset = district_queryset.filter(
            Q(name__icontains=query) | Q(state__name__icontains=query)
        )

    context = {
        'form': form,
        'query': query,
        'states': State.objects.all(),
        'selected_state_id': state_id,
        'districts': Paginator(district_queryset, 100).get_page(request.GET.get('page')),
    }
    return render(request, 'locations/district_list.html', context)


@role_required(User.UserRole.ADMIN)
def district_edit(request, district_id):
    district = get_object_or_404(District, id=district_id)
    if request.method == 'POST':
        form = DistrictForm(request.POST, instance=district)
        if form.is_valid():
            district = form.save()
            if district.is_active and district.state and not district.state.is_active:
                district.is_active = False
                district.save(update_fields=['is_active'])
                messages.error(
                    request,
                    'Cannot set this district as Available because its state is Not Available.',
                )
            if not district.is_active:
                Location.objects.filter(district=district, is_active=True).update(is_active=False)
                messages.info(
                    request,
                    f'All locations under {district.name} were marked Not Available for service.',
                )
            else:
                restored = Location.objects.filter(district=district, is_active=False).update(is_active=True)
                if restored:
                    messages.success(
                        request,
                        f'{restored} locations/pincodes in {district.name} were restored to Available.',
                    )
            return redirect('locations:district_list')
    else:
        form = DistrictForm(instance=district)
    return render(request, 'locations/district_edit.html', {'form': form, 'district': district})


@role_required(User.UserRole.ADMIN)
def district_toggle_availability(request, district_id):
    district = get_object_or_404(District, id=district_id)
    if request.method == 'POST':
        desired_active = request.POST.get('is_active') == 'on'
        if desired_active and district.state and not district.state.is_active:
            district.is_active = False
            messages.error(
                request,
                'Cannot set this district as Available because its state is Not Available.',
            )
        else:
            district.is_active = desired_active
        district.save(update_fields=['is_active'])
        if not district.is_active:
            affected = Location.objects.filter(district=district, is_active=True).update(is_active=False)
            if affected:
                messages.info(
                    request,
                    f'{affected} locations/pincodes in {district.name} were set to Not Available.',
                )
        else:
            restored = Location.objects.filter(district=district, is_active=False).update(is_active=True)
            if restored:
                messages.success(
                    request,
                    f'{restored} locations/pincodes in {district.name} were restored to Available.',
                )
    return redirect(request.META.get('HTTP_REFERER', 'locations:district_list'))


@role_required(User.UserRole.ADMIN)
def location_list_create(request):
    if request.method == 'POST':
        form = LocationForm(request.POST)
        if form.is_valid():
            location = form.save(commit=False)
            if location.is_active and (
                not location.district.is_active
                or (location.district.state and not location.district.state.is_active)
            ):
                location.is_active = False
                messages.error(
                    request,
                    'Cannot set this location as Available because district/state is Not Available.',
                )
            location.save()
            return redirect('locations:location_list')
    else:
        form = LocationForm()

    query = request.GET.get('q', '').strip()
    state_id = (request.GET.get('state') or '').strip()
    district_id = (request.GET.get('district') or '').strip()
    location_queryset = Location.objects.select_related('district', 'district__state').all()
    if state_id:
        location_queryset = location_queryset.filter(district__state_id=state_id)
    if district_id:
        location_queryset = location_queryset.filter(district_id=district_id)
    if query:
        location_queryset = location_queryset.filter(
            Q(name__icontains=query)
            | Q(postal_code__icontains=query)
            | Q(district__name__icontains=query)
            | Q(district__state__name__icontains=query)
        )

    district_options = District.objects.select_related('state').all()
    if state_id:
        district_options = district_options.filter(state_id=state_id)

    context = {
        'form': form,
        'query': query,
        'states': State.objects.all(),
        'district_options': district_options,
        'selected_state_id': state_id,
        'selected_district_id': district_id,
        'locations': Paginator(location_queryset, 150).get_page(request.GET.get('page')),
    }
    return render(request, 'locations/location_list.html', context)


@role_required(User.UserRole.ADMIN)
def non_servicing_pincode_list(request):
    query = request.GET.get('q', '').strip()
    state_id = (request.GET.get('state') or '').strip()
    district_id = (request.GET.get('district') or '').strip()

    location_queryset = Location.objects.select_related('district', 'district__state').filter(is_active=False)

    if state_id:
        location_queryset = location_queryset.filter(district__state_id=state_id)
    if district_id:
        location_queryset = location_queryset.filter(district_id=district_id)
    if query:
        location_queryset = location_queryset.filter(
            Q(name__icontains=query)
            | Q(postal_code__icontains=query)
            | Q(district__name__icontains=query)
            | Q(district__state__name__icontains=query)
        )

    district_options = District.objects.select_related('state').all()
    if state_id:
        district_options = district_options.filter(state_id=state_id)

    context = {
        'query': query,
        'states': State.objects.all(),
        'district_options': district_options,
        'selected_state_id': state_id,
        'selected_district_id': district_id,
        'locations': Paginator(location_queryset, 150).get_page(request.GET.get('page')),
    }
    return render(request, 'locations/non_servicing_pincodes.html', context)


@role_required(User.UserRole.ADMIN)
def location_edit(request, location_id):
    location = get_object_or_404(Location, id=location_id)
    if request.method == 'POST':
        form = LocationForm(request.POST, instance=location)
        if form.is_valid():
            location = form.save(commit=False)
            if location.is_active and (
                not location.district.is_active
                or (location.district.state and not location.district.state.is_active)
            ):
                location.is_active = False
                messages.error(
                    request,
                    'Cannot set this location as Available because district/state is Not Available.',
                )
            location.save()
            return redirect('locations:location_list')
    else:
        form = LocationForm(instance=location)
    return render(request, 'locations/location_edit.html', {'form': form, 'location': location})


@role_required(User.UserRole.ADMIN)
def location_toggle_availability(request, location_id):
    location = get_object_or_404(Location, id=location_id)
    if request.method == 'POST':
        desired_active = request.POST.get('is_active') == 'on'
        if desired_active and (
            not location.district.is_active
            or (location.district.state and not location.district.state.is_active)
        ):
            location.is_active = False
            messages.error(
                request,
                'Cannot set this location as Available because district/state is Not Available.',
            )
        else:
            location.is_active = desired_active
        location.save(update_fields=['is_active'])
    return redirect(request.META.get('HTTP_REFERER', 'locations:location_list'))

# Create your views here.
