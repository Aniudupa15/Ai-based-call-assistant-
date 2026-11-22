package com.ani.callassistant.data.db

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow

/**
 * Local cache of the PC's records, so history is readable with no connection.
 * The PC is the source of truth; this holds nothing the PC does not.
 */
@Entity(tableName = "calls")
data class CallEntity(
    @PrimaryKey val id: String,
    val startedAt: Long,
    val json: String,            // the call as returned by the API
    val hasDetail: Boolean,      // json includes turns and commitments
)

@Dao
interface CallDao {
    @Query("SELECT * FROM calls ORDER BY startedAt DESC")
    fun all(): Flow<List<CallEntity>>

    @Query("SELECT * FROM calls WHERE id = :id")
    fun byId(id: String): Flow<CallEntity?>

    @Query("SELECT * FROM calls WHERE id = :id")
    suspend fun get(id: String): CallEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(items: List<CallEntity>)

    @Query("DELETE FROM calls WHERE id = :id")
    suspend fun delete(id: String)

    @Query("DELETE FROM calls WHERE id NOT IN (:keep)")
    suspend fun deleteAllExcept(keep: List<String>)
}

@Database(entities = [CallEntity::class], version = 1, exportSchema = false)
abstract class CallDatabase : RoomDatabase() {
    abstract fun calls(): CallDao

    companion object {
        fun build(context: Context): CallDatabase =
            Room.databaseBuilder(context, CallDatabase::class.java, "calls.db")
                .fallbackToDestructiveMigration()
                .build()
    }
}
