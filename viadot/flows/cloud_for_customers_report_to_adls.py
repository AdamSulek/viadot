import os
from typing import Any, Dict, List, Union

import pandas as pd
import pendulum
from prefect import Flow, Task, apply_map
from prefect.backend import set_key_value
from prefect.utilities import logging


from ..task_utils import (
    add_ingestion_metadata_task,
    union_dfs_task,
    df_to_csv_task,
    df_to_parquet_task,
)

from ..tasks import AzureDataLakeUpload, c4c_report_to_df, c4c_to_df

logger = logging.get_logger(__name__)

file_to_adls_task = AzureDataLakeUpload()


class CloudForCustomersReportToADLS(Flow):
    def __init__(
        self,
        report_url: str = None,
        url: str = None,
        env: str = "QA",
        endpoint: str = None,
        params: Dict[str, Any] = {},
        fields: List[str] = None,
        name: str = None,
        adls_sp_credentials_secret: str = None,
        local_file_path: str = None,
        output_file_extension: str = ".csv",
        adls_dir_path: str = None,
        if_empty: str = "warn",
        if_exists: str = "replace",
        skip: int = 0,
        top: int = 1000,
        channels: List[str] = None,
        months: List[str] = None,
        years: List[str] = None,
        *args: List[any],
        **kwargs: Dict[str, Any],
    ):
        """
        Flow for downloading data from different marketing APIs to a local CSV
        using Cloud for Customers API, then uploading it to Azure Data Lake.

        Args:
            report_url (str, optional): The url to the API. Defaults to None.
            name (str): The name of the flow.
            adls_sp_credentials_secret (str, optional): The name of the Azure Key Vault secret containing a dictionary with
            ACCOUNT_NAME and Service Principal credentials (TENANT_ID, CLIENT_ID, CLIENT_SECRET) for the Azure Data Lake.
            Defaults to None.
            local_file_path (str, optional): Local destination path. Defaults to None.
            output_file_extension (str, optional): Output file extension - to allow selection of .csv for data which is not easy
            to handle with parquet. Defaults to ".csv".
            adls_dir_path (str, optional): Azure Data Lake destination folder/catalog path. Defaults to None.
            if_empty (str, optional): What to do if the Supermetrics query returns no data. Defaults to "warn".
            if_exists (str, optional): What to do if the table already exists. Defaults to "replace".
            skip (int, optional): Initial index value of reading row.
            top (int, optional): The value of top reading row.
            channels (List[str], optional): Filtering parameters passed to the url.
            months (List[str], optional): Filtering parameters passed to the url.
            years (List[str], optional): Filtering parameters passed to the url.
        """

        self.if_empty = if_empty
        self.report_url = report_url
        self.env = env
        self.skip = skip
        self.top = top
        # AzureDataLakeUpload
        self.adls_sp_credentials_secret = adls_sp_credentials_secret
        self.if_exists = if_exists
        self.output_file_extension = output_file_extension
        self.local_file_path = (
            local_file_path or self.slugify(name) + self.output_file_extension
        )
        self.now = str(pendulum.now("utc"))
        self.adls_dir_path = adls_dir_path
        self.adls_file_path = os.path.join(
            adls_dir_path, self.now + self.output_file_extension
        )
        # in case of non-report invoking
        self.url = url
        self.endpoint = endpoint
        self.params = params
        self.fields = fields
        # filtering for report_url for reports
        self.channels = channels
        self.months = months
        self.years = years

        self.report_urls_with_filters = [self.report_url]

        self.report_urls_with_filters = self.create_url_with_fields(
            fields_list=self.channels, filter_code="CCHANNETZTEXT12CE6C2FA0D77995"
        )

        self.report_urls_with_filters = self.create_url_with_fields(
            fields_list=self.months, filter_code="CMONTH_ID"
        )

        self.report_urls_with_filters = self.create_url_with_fields(
            fields_list=self.years, filter_code="CYEAR_ID"
        )

        super().__init__(*args, name=name, **kwargs)

        self.gen_flow()

    def create_url_with_fields(self, fields_list: List[str], filter_code: str) -> List:
        urls_list_result = []
        add_filter = True
        if len(self.report_urls_with_filters) > 1:
            add_filter = False

        if fields_list:
            for url in self.report_urls_with_filters:
                for field in fields_list:
                    if add_filter:
                        new_url = f"{url}&$filter=({filter_code}%20eq%20%27{field}%27)"
                    elif not add_filter:
                        new_url = f"{url}%20and%20({filter_code}%20eq%20%27{field}%27)"
                    urls_list_result.append(new_url)
            return urls_list_result
        else:
            return self.report_urls_with_filters

    @staticmethod
    def slugify(name):
        return name.replace(" ", "_").lower()

    def gen_c4c(
        self,
        url: str,
        report_url: str,
        endpoint: str,
        params: str,
        env: str,
        flow: Flow = None,
    ) -> Task:

        df = c4c_to_df.bind(
            url=url,
            env=env,
            endpoint=endpoint,
            params=params,
            report_url=report_url,
            flow=flow,
        )

        return df

    def gen_c4c_report_months(
        self, report_urls_with_filters: Union[str, List[str]], flow: Flow = None
    ) -> Task:

        report = c4c_report_to_df.bind(
            skip=self.skip,
            top=self.top,
            report_url=report_urls_with_filters,
            env=self.env,
            flow=flow,
        )

        return report

    def gen_flow(self) -> Flow:
        if self.report_url:
            dfs = apply_map(
                self.gen_c4c_report_months, self.report_urls_with_filters, flow=self
            )
            df = union_dfs_task.bind(dfs, flow=self)
        elif self.url:
            df = self.gen_c4c(
                url=self.url,
                report_url=self.report_url,
                env=self.env,
                endpoint=self.endpoint,
                params=self.params,
                flow=self,
            )

        df_with_metadata = add_ingestion_metadata_task.bind(df, flow=self)

        if self.output_file_extension == ".parquet":
            df_to_file = df_to_parquet_task.bind(
                df=df_with_metadata,
                path=self.local_file_path,
                if_exists=self.if_exists,
                flow=self,
            )
        else:
            df_to_file = df_to_csv_task.bind(
                df=df_with_metadata,
                path=self.local_file_path,
                if_exists=self.if_exists,
                flow=self,
            )

        file_to_adls_task.bind(
            from_path=self.local_file_path,
            to_path=self.adls_file_path,
            sp_credentials_secret=self.adls_sp_credentials_secret,
            flow=self,
        )

        df_with_metadata.set_upstream(df, flow=self)
        df_to_file.set_upstream(df_with_metadata, flow=self)
        file_to_adls_task.set_upstream(df_to_file, flow=self)

        set_key_value(key=self.adls_dir_path, value=self.adls_file_path)
