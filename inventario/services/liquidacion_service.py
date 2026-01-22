from datetime import datetime, timedelta
from django.db import connection
from django.http import HttpResponse
import pandas as pd
from io import BytesIO
import xlsxwriter

from ..models import Sede, Producto, Stock
from ..models_liquidacion import LiquidacionSemanal, LiquidacionLog


class LiquidacionService:
    """
    Servicio para gestionar operaciones de liquidación de inventario
    """
    
    def __init__(self):
        pass
    
    def liquidar_sede(self, sede_id, semana, anio, usuario_id, observaciones=None):
        """
        Ejecutar liquidación de una sede específica
        """
        try:
            with connection.cursor() as cursor:
                # Ejecutar función SQL de liquidación
                cursor.execute(
                    "SELECT liquidar_sede(%s, %s, %s, %s, %s)",
                    [sede_id, semana, anio, usuario_id, observaciones]
                )
                
                # Obtener resultados
                cursor.execute("""
                    SELECT 
                        p.id as producto_id,
                        p.nombre as producto_nombre,
                        ls.stock_inicial,
                        ls.stock_final,
                        ls.cantidad_entregada,
                        ls.cantidad_usada,
                        ls.cantidad_devuelta,
                        ls.cantidad_merma,
                        ls.diferencia,
                        CASE WHEN ls.diferencia = 0 THEN 'OK' ELSE 'DISCREPANCIA' END as estado
                    FROM inventario_liquidacion_semanal ls
                    INNER JOIN inventario_producto p ON ls.producto_id = p.id
                    WHERE ls.semana = %s AND ls.anio = %s AND ls.sede_id = %s
                    ORDER BY p.nombre
                """, [semana, anio, sede_id])
                
                columns = [desc[0] for desc in cursor.description]
                results = cursor.fetchall()
                
                # Convertir a lista de diccionarios
                datos = [dict(zip(columns, row)) for row in results]
                
                # Registrar log
                self._registrar_log(
                    tipo='LIQUIDACION_SEDE',
                    semana=semana,
                    anio=anio,
                    sede_id=sede_id,
                    usuario_id=usuario_id,
                    descripcion=f"Liquidación de sede {Sede.objects.get(id=sede_id).nombre}",
                    productos_procesados=len(datos),
                    discrepancias_detectadas=len([d for d in datos if d['diferencia'] != 0])
                )
                
                return datos
                
        except Exception as e:
            raise Exception(f"Error en liquidación de sede: {str(e)}")
    
    def liquidar_central(self, semana, anio, usuario_id, observaciones=None):
        """
        Ejecutar liquidación del almacén central
        """
        try:
            with connection.cursor() as cursor:
                # Ejecutar función SQL de liquidación central
                cursor.execute(
                    "SELECT liquidar_almacen_central(%s, %s, %s, %s)",
                    [semana, anio, usuario_id, observaciones]
                )
                
                # Obtener resultados
                cursor.execute("""
                    SELECT 
                        p.id as producto_id,
                        p.nombre as producto_nombre,
                        ls.stock_inicial,
                        ls.stock_final,
                        ls.cantidad_entregada as total_entregado_sedes,
                        ls.cantidad_usada as total_usado_sedes,
                        ls.cantidad_devuelta as total_devuelto_sedes,
                        ls.cantidad_merma as total_merma_sedes,
                        ls.diferencia as diferencia_central,
                        ls.estado as consistencia_global,
                        CASE WHEN ls.diferencia = 0 THEN 'OK' ELSE 'DISCREPANCIA' END as estado
                    FROM inventario_liquidacion_semanal ls
                    INNER JOIN inventario_producto p ON ls.producto_id = p.id
                    INNER JOIN inventario_sede s ON ls.sede_id = s.id
                    WHERE ls.semana = %s AND ls.anio = %s AND s.tipo = 'CENTRAL'
                    ORDER BY p.nombre
                """, [semana, anio])
                
                columns = [desc[0] for desc in cursor.description]
                results = cursor.fetchall()
                
                # Convertir a lista de diccionarios
                datos = [dict(zip(columns, row)) for row in results]
                
                # Registrar log
                self._registrar_log(
                    tipo='LIQUIDACION_CENTRAL',
                    semana=semana,
                    anio=anio,
                    usuario_id=usuario_id,
                    descripcion="Liquidación de almacén central - verificación global",
                    productos_procesados=len(datos),
                    discrepancias_detectadas=len([d for d in datos if d['consistencia_global'] == 'INCONSISTENTE'])
                )
                
                return datos
                
        except Exception as e:
            raise Exception(f"Error en liquidación central: {str(e)}")
    
    def obtener_resumen(self, semana=None, anio=None, sede_id=None):
        """
        Obtener resumen estadístico de liquidación
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM resumen_liquidacion(%s, %s, %s)",
                    [semana, anio, sede_id]
                )
                
                columns = [desc[0] for desc in cursor.description]
                result = cursor.fetchone()
                
                if result:
                    return dict(zip(columns, result))
                else:
                    return {
                        'total_productos': 0,
                        'productos_con_diferencia': 0,
                        'total_stock_inicial': 0,
                        'total_stock_final': 0,
                        'total_entregado': 0,
                        'total_usado': 0,
                        'total_devuelto': 0,
                        'total_merma': 0,
                        'total_diferencia': 0,
                        'porcentaje_diferencia': 0.0,
                        'estado_general': 'PERFECTO'
                    }
                    
        except Exception as e:
            raise Exception(f"Error al obtener resumen: {str(e)}")
    
    def obtener_liquidaciones_filtradas(self, filtro_tipo=None, filtro_valor=None, sede_id=None, producto_id=None):
        """
        Obtener liquidaciones con filtros aplicados
        """
        query = """
            SELECT 
                ls.id,
                ls.fecha_liquidacion,
                ls.semana,
                ls.anio,
                EXTRACT(YEAR FROM ls.fecha_liquidacion)::INTEGER AS anio_calendario,
                EXTRACT(MONTH FROM ls.fecha_liquidacion)::INTEGER AS mes,
                EXTRACT(DAY FROM ls.fecha_liquidacion)::INTEGER AS dia,
                s.nombre AS sede_nombre,
                s.tipo AS sede_tipo,
                p.nombre AS producto_nombre,
                p.codigo_interno AS producto_codigo,
                p.unidad AS producto_unidad,
                ls.stock_inicial,
                ls.stock_final,
                ls.cantidad_entregada,
                ls.cantidad_usada,
                ls.cantidad_devuelta,
                ls.cantidad_merma,
                ls.diferencia,
                ls.estado,
                ls.observaciones,
                u.username AS liquidado_por,
                ls.creado_en,
                ls.actualizado_en,
                (ls.stock_inicial - ls.stock_final) AS variacion_stock,
                (ls.cantidad_entregada - ls.cantidad_devuelta) AS movimiento_neto,
                CASE 
                    WHEN ls.diferencia = 0 THEN 'BALANCEADO'
                    WHEN ls.diferencia > 0 THEN 'SOBRANTE'
                    ELSE 'FALTANTE'
                END AS tipo_diferencia,
                CASE 
                    WHEN ls.stock_inicial > 0 THEN 
                        ROUND((ls.cantidad_usada::NUMERIC / ls.stock_inicial::NUMERIC) * 100, 2)
                    ELSE 0
                END AS porcentaje_usado,
                CASE 
                    WHEN ls.stock_inicial > 0 THEN 
                        ROUND((ls.cantidad_merma::NUMERIC / ls.stock_inicial::NUMERIC) * 100, 2)
                    ELSE 0
                END AS porcentaje_merma
            FROM inventario_liquidacion_semanal ls
            INNER JOIN inventario_sede s ON ls.sede_id = s.id
            INNER JOIN inventario_producto p ON ls.producto_id = p.id
            LEFT JOIN auth_user u ON ls.liquidado_por_id = u.id
            WHERE 1=1
        """
        
        params = []
        
        if filtro_tipo == 'dia' and filtro_valor:
            query += " AND EXTRACT(DAY FROM ls.fecha_liquidacion) = %s"
            params.append(filtro_valor)
        elif filtro_tipo == 'semana' and filtro_valor:
            query += " AND ls.semana = %s"
            params.append(filtro_valor)
        elif filtro_tipo == 'mes' and filtro_valor:
            query += " AND EXTRACT(MONTH FROM ls.fecha_liquidacion) = %s"
            params.append(filtro_valor)
        elif filtro_tipo == 'anio' and filtro_valor:
            query += " AND EXTRACT(YEAR FROM ls.fecha_liquidacion) = %s"
            params.append(filtro_valor)
        
        if sede_id:
            query += " AND ls.sede_id = %s"
            params.append(sede_id)
            
        if producto_id:
            query += " AND ls.producto_id = %s"
            params.append(producto_id)
        
        query += " ORDER BY ls.fecha_liquidacion DESC, s.nombre, p.nombre"
        
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            results = cursor.fetchall()
            
            # Convertir a lista de diccionarios
            return [dict(zip(columns, row)) for row in results]
    
    def obtener_datos_grafico_stock(self, filtro_tipo=None, filtro_valor=None):
        """
        Obtener datos para gráfico de stock
        """
        liquidaciones = self.obtener_liquidaciones_filtradas(filtro_tipo, filtro_valor)
        
        # Tomar primeros 5 productos para el gráfico
        datos_grafico = liquidaciones[:5]
        
        return {
            'labels': [item['producto_nombre'] for item in datos_grafico],
            'stock_inicial': [item['stock_inicial'] for item in datos_grafico],
            'stock_final': [item['stock_final'] for item in datos_grafico],
        }
    
    def obtener_datos_grafico_sedes(self, filtro_tipo=None, filtro_valor=None):
        """
        Obtener datos para gráfico de sedes
        """
        liquidaciones = self.obtener_liquidaciones_filtradas(filtro_tipo, filtro_valor)
        
        # Agrupar por sede
        sedes_data = {}
        for item in liquidaciones:
            sede = item['sede_nombre']
            if sede not in sedes_data:
                sedes_data[sede] = 0
            sedes_data[sede] += item['stock_final']
        
        return {
            'labels': list(sedes_data.keys()),
            'data': list(sedes_data.values()),
        }
    
    def exportar_excel(self, liquidaciones):
        """
        Exportar liquidaciones a formato Excel
        """
        # Crear DataFrame
        df = pd.DataFrame(liquidaciones)
        
        # Crear archivo Excel en memoria
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output)
        worksheet = workbook.add_worksheet('Liquidación')
        
        # Formatos
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#4F81BD',
            'font_color': 'white',
            'border': 1
        })
        
        number_format = workbook.add_format({'num_format': '#,##0'})
        percent_format = workbook.add_format({'num_format': '0.00%'})
        
        # Escribir encabezados
        headers = [
            'Fecha', 'Semana', 'Año', 'Sede', 'Producto', 'Código',
            'Stock Inicial', 'Stock Final', 'Entregado', 'Usado', 'Devuelto',
            'Merma', 'Diferencia', 'Estado', 'Porcentaje Usado', 'Porcentaje Merma'
        ]
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
        
        # Escribir datos
        for row, item in enumerate(liquidaciones, 1):
            worksheet.write(row, 0, item['fecha_liquidacion'])
            worksheet.write(row, 1, item['semana'])
            worksheet.write(row, 2, item['anio'])
            worksheet.write(row, 3, item['sede_nombre'])
            worksheet.write(row, 4, item['producto_nombre'])
            worksheet.write(row, 5, item['producto_codigo'])
            worksheet.write(row, 6, item['stock_inicial'], number_format)
            worksheet.write(row, 7, item['stock_final'], number_format)
            worksheet.write(row, 8, item['cantidad_entregada'], number_format)
            worksheet.write(row, 9, item['cantidad_usada'], number_format)
            worksheet.write(row, 10, item['cantidad_devuelta'], number_format)
            worksheet.write(row, 11, item['cantidad_merma'], number_format)
            worksheet.write(row, 12, item['diferencia'], number_format)
            worksheet.write(row, 13, item['estado'])
            worksheet.write(row, 14, item['porcentaje_usado'] / 100, percent_format)
            worksheet.write(row, 15, item['porcentaje_merma'] / 100, percent_format)
        
        # Ajustar anchos de columna
        worksheet.set_column(0, 15, 15)
        
        workbook.close()
        
        # Preparar respuesta HTTP
        output.seek(0)
        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename=liquidacion_inventario.xlsx'
        
        return response
    
    def _registrar_log(self, tipo, semana, anio, usuario_id, descripcion, 
                      productos_procesados=0, discrepancias_detectadas=0, sede_id=None):
        """
        Registrar log de operación de liquidación
        """
        try:
            LiquidacionLog.objects.create(
                tipo=tipo,
                semana=semana,
                anio=anio,
                sede_id=sede_id,
                usuario_id=usuario_id,
                descripcion=descripcion,
                productos_procesados=productos_procesados,
                discrepancias_detectadas=discrepancias_detectadas
            )
        except Exception as e:
            # Si falla el log, no debe afectar la operación principal
            print(f"Error al registrar log: {str(e)}")
