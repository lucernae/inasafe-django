# coding=utf-8
from __future__ import absolute_import

import logging
import os
import shutil
from tempfile import mkdtemp

import pytz
from celery.result import AsyncResult
from django.core.files import File

from core.celery_app import app
from realtime.app_settings import LOGGER_NAME, REALTIME_HAZARD_DROP, \
    ASH_LAYER_ORDER, ASH_REPORT_TEMPLATE, ASH_EXPOSURES, ASH_AGGREGATION
from realtime.models.ash import Ash, AshReport
from realtime.tasks.headless.celery_app import app as headless_app
from realtime.tasks.headless.inasafe_wrapper import (
    run_multi_exposure_analysis, generate_report)
from realtime.tasks.realtime.ash import process_ash
from realtime.tasks.realtime.celery_app import app as realtime_app

__author__ = 'Rizky Maulana Nugraha <lana.pcfre@gmail.com>'
__date__ = '20/7/17'


LOGGER = logging.getLogger(LOGGER_NAME)


@app.task(queue='inasafe-django')
def check_processing_task():
    # Checking ash processing task
    for ash in Ash.objects.exclude(
            task_id__isnull=True).exclude(
            task_id__exact='').exclude(
            task_status__iexact='SUCCESS'):
        task_id = ash.task_id
        result = AsyncResult(id=task_id, app=realtime_app)
        ash.task_status = result.state
        ash.save()
    # Checking analysis task
    for ash in Ash.objects.exclude(
            analysis_task_id__isnull=True).exclude(
            analysis_task_id__exact='').exclude(
            analysis_task_status__iexact='SUCCESS'):
        analysis_task_id = ash.analysis_task_id
        result = AsyncResult(id=analysis_task_id, app=headless_app)
        ash.analysis_task_status = result.state
        # Set the impact file path if success
        if ash.analysis_task_status == 'SUCCESS':
            ash.impact_file_path = result.result['output']['analysis_summary']
        ash.save()
    # Checking report generation task
    for ash in Ash.objects.exclude(
            report_task_id__isnull=True).exclude(
            report_task_id__exact='').exclude(
            report_task_status__iexact='SUCCESS'):
        report_task_id = ash.report_task_id
        result = AsyncResult(id=report_task_id, app=headless_app)
        ash.report_task_status = result.state
        # Set the report path if success
        if ash.report_task_status == 'SUCCESS':
            report_path = result.result[
                'output']['pdf_product_tag']['realtime-ash-en']
            # Create ash report object
            # Set the language manually first
            ash_report = AshReport(ash=ash, language='en')
            with open(report_path, 'rb') as report_file:
                ash_report.report_map.save(
                    ash_report.report_map_filename,
                    File(report_file),
                    save=True)
            ash_report.save()
        ash.save()


@app.task(queue='inasafe-django')
def generate_event_report(ash_event):
    """Generate Ash Report

    :param ash_event: Ash event instance
    :type ash_event: Ash
    :return:
    """
    ash_event.use_timezone()

    if ash_event.event_time.tzinfo:
        event_time = ash_event.event_time
    else:
        event_time = ash_event.event_time.replace(tzinfo=pytz.utc)

    # Check hazard layer
    if not ash_event.hazard_layer_exists and ash_event.need_generate_hazard:

        # For ash realtime we need to make sure hazard file is processed.
        # Because the hazard raw data comes from user upload

        # copy hazard data realtime location
        hazard_drop_path = mkdtemp(dir=REALTIME_HAZARD_DROP)
        hazard_drop_path = os.path.join(
            hazard_drop_path, os.path.basename(ash_event.hazard_file.path))
        shutil.copy(ash_event.hazard_file.path, hazard_drop_path)

        LOGGER.info('Sending task ash hazard processing.')
        result = process_ash.delay(
            ash_file_path=hazard_drop_path,
            volcano_name=ash_event.volcano.volcano_name,
            region=ash_event.volcano.province,
            latitude=ash_event.volcano.location[1],
            longitude=ash_event.volcano.location[0],
            alert_level=ash_event.alert_level,
            event_time=event_time,
            eruption_height=ash_event.eruption_height,
            vent_height=ash_event.volcano.elevation,
            forecast_duration=ash_event.forecast_duration)

        Ash.objects.filter(id=ash_event.id).update(
            task_id=result.task_id,
            task_status=result.state)

    # Check impact layer
    elif (ash_event.hazard_layer_exists and
          not ash_event.impact_layer_exists and
          ash_event.need_run_analysis):

        # If hazard exists but impact layer is not, then create a new analysis
        # job.
        run_ash_analysis(ash_event)

    # Check report
    elif (ash_event.hazard_layer_exists and
          ash_event.impact_layer_exists and
          not ash_event.has_reports and
          ash_event.need_generate_reports):

        # If analysis is done but report doesn't exists, then create the
        # reports.
        generate_ash_report(ash_event)


def run_ash_analysis(ash_event):
    """Run ash analysis.

    :param ash_event: Ash event instance
    :type ash_event: Ash
    """
    ash_layer_uri = ash_event.hazard_path
    async_result = run_multi_exposure_analysis.delay(
        ash_layer_uri,
        ASH_EXPOSURES,
        ASH_AGGREGATION,
    )
    Ash.objects.filter(id=ash_event.id).update(
        analysis_task_id=async_result.task_id,
        analysis_task_status=async_result.state)


def generate_ash_report(ash_event):
    """Generate ash report for ash event.

    :param ash_event: Ash event instance
    :type ash_event: Ash
    """
    ash_impact_layer_uri = ash_event.impact_file_path
    layer_order = [
        ASH_LAYER_ORDER[0],
        ash_event.hazard_path,
        ASH_LAYER_ORDER[1]
    ]
    async_result = generate_report.delay(
        ash_impact_layer_uri, ASH_REPORT_TEMPLATE, layer_order)
    Ash.objects.filter(id=ash_event.id).update(
        report_task_id=async_result.task_id,
        report_task_status=async_result.state)
